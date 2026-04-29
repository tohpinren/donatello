"""Pipeline orchestration for dry runs, normalization, and mutation generation."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlmt.artifacts import ExperimentArtifacts, artifacts_for
from sqlmt.augmentation import run_augmentation as _run_augmentation
from sqlmt.benchmarks import BenchmarkError, normalize_examples
from sqlmt.config import ExperimentConfig
from sqlmt.counterexamples import (
    run_counterexample_generation as _run_counterexample_generation,
)
from sqlmt.equivalence import run_equivalence_filtering as _run_equivalence_filtering
from sqlmt.execution import run_sqlite_execution as _run_sqlite_execution
from sqlmt.models import (
    AugmentationSummary,
    CounterexampleSummary,
    ExecutionSummary,
    MutationSummary,
    NormalizationSummary,
    NormalizedExample,
    ReportSummary,
)
from sqlmt.mutation.generator import (
    generate_mutations,
    read_mutation_records,
    read_normalized_examples,
    summarize_mutations,
    write_mutation_records,
)
from sqlmt.reporting import run_reporting as _run_reporting

LOGGER = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """Raised when the configured pipeline cannot be executed."""


@dataclass(frozen=True)
class DryRunPlan:
    """Resolved plan shown to the user for a side-effect-free dry run."""

    experiment_name: str
    output_dir: str
    enabled_stages: list[str]
    artifacts: dict[str, str]


def build_dry_run_plan(config: ExperimentConfig) -> DryRunPlan:
    """Resolve artifacts and enabled stages without touching the filesystem."""

    artifacts = artifacts_for(config.output_dir)
    return DryRunPlan(
        experiment_name=config.experiment_name,
        output_dir=str(artifacts.root),
        enabled_stages=config.stages.enabled(),
        artifacts=artifacts.as_display_dict(),
    )


def prepare_run(config: ExperimentConfig) -> ExperimentArtifacts:
    """Create the artifact root and return the resolved path wrapper."""

    artifacts = artifacts_for(config.output_dir)
    artifacts.ensure_base_dirs()
    LOGGER.info("Prepared artifact root=%s", artifacts.root)
    return artifacts


def preflight_run(config: ExperimentConfig) -> None:
    """Reject unsupported or no-op runs before creating output artifacts."""

    LOGGER.info(
        "Preflight start benchmark=%s mode=%s output_dir=%s stages=%s",
        config.benchmark,
        config.bird_mode,
        config.output_dir,
        ",".join(config.stages.enabled()),
    )
    if config.benchmark != "bird":
        raise PipelineError(
            f"Normalization for benchmark '{config.benchmark}' is not implemented yet."
        )
    if (
        not config.stages.normalize
        and not config.stages.mutate
        and not config.stages.equivalence
        and not config.stages.execute
        and not config.stages.counterexamples
        and not config.stages.augment
        and not config.stages.report
    ):
        raise PipelineError(
            "No implemented stage is enabled. "
            "Set stages.normalize: true, stages.mutate: true, "
            "stages.equivalence: true, stages.execute: true, "
            "stages.counterexamples: true, stages.augment: true, "
            "or stages.report: true."
        )
    if config.stages.mutate and not config.stages.normalize:
        artifacts = artifacts_for(config.output_dir)
        if not artifacts.normalized_examples.exists():
            raise PipelineError(
                "Mutation generation requires normalized_examples.jsonl when "
                "stages.normalize is false, but normalized_examples.jsonl "
                "does not exist."
            )
    if config.stages.equivalence and not config.stages.mutate:
        artifacts = artifacts_for(config.output_dir)
        if not artifacts.mutants.exists():
            raise PipelineError(
                "Equivalence filtering requires mutants.jsonl when "
                "stages.mutate is false, but mutants.jsonl does not exist."
            )
        if not artifacts.normalized_examples.exists():
            raise PipelineError(
                "Equivalence filtering requires normalized_examples.jsonl when "
                "stages.normalize is false, but normalized_examples.jsonl "
                "does not exist."
            )
    if config.stages.execute and not config.stages.equivalence:
        artifacts = artifacts_for(config.output_dir)
        if not artifacts.mutants.exists():
            raise PipelineError(
                "SQLite execution requires mutants.jsonl when "
                "stages.mutate is false, but mutants.jsonl does not exist."
            )
        if not artifacts.equivalence.exists():
            raise PipelineError(
                "SQLite execution requires equivalence.jsonl when "
                "stages.equivalence is false, but equivalence.jsonl does not exist."
            )
    if config.stages.counterexamples:
        artifacts = artifacts_for(config.output_dir)
        if not config.stages.mutate and not artifacts.mutants.exists():
            raise PipelineError(
                "Counterexample generation requires mutants.jsonl when "
                "stages.mutate is false, but mutants.jsonl does not exist."
            )
        if not config.stages.equivalence and not artifacts.equivalence.exists():
            raise PipelineError(
                "Counterexample generation requires equivalence.jsonl when "
                "stages.equivalence is false, but equivalence.jsonl does not exist."
            )
        if not config.stages.execute and not artifacts.execution.exists():
            raise PipelineError(
                "Counterexample generation requires execution.jsonl when "
                "stages.execute is false, but execution.jsonl does not exist."
            )
    if config.stages.augment:
        artifacts = artifacts_for(config.output_dir)
        if not config.stages.mutate and not artifacts.mutants.exists():
            raise PipelineError(
                "Augmentation requires mutants.jsonl when stages.mutate is false, "
                "but mutants.jsonl does not exist."
            )
        if not config.stages.equivalence and not artifacts.equivalence.exists():
            raise PipelineError(
                "Augmentation requires equivalence.jsonl when "
                "stages.equivalence is false, but equivalence.jsonl does not exist."
            )
        if not config.stages.execute and not artifacts.execution.exists():
            raise PipelineError(
                "Augmentation requires execution.jsonl when stages.execute is false, "
                "but execution.jsonl does not exist."
            )
        if not config.stages.counterexamples and not artifacts.counterexamples.exists():
            raise PipelineError(
                "Augmentation requires counterexamples.jsonl when "
                "stages.counterexamples is false, but counterexamples.jsonl "
                "does not exist."
            )


def run_equivalence_filtering(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
):
    """Execute or resume VeriEQL equivalence filtering."""

    try:
        return _run_equivalence_filtering(config, artifacts=artifacts)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc


def run_sqlite_execution(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
) -> ExecutionSummary:
    """Execute or resume SQLite mutation testing."""

    try:
        return _run_sqlite_execution(config, artifacts=artifacts)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc


def run_counterexample_generation(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
) -> CounterexampleSummary:
    """Execute or resume SQLDriller counterexample generation."""

    try:
        return _run_counterexample_generation(config, artifacts=artifacts)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc


def run_augmentation(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
) -> AugmentationSummary:
    """Execute or resume database augmentation and rerun verification."""

    try:
        return _run_augmentation(config, artifacts=artifacts)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc


def run_reporting(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
) -> ReportSummary:
    """Generate Markdown and JSON reports from available artifacts."""

    try:
        return _run_reporting(config, artifacts=artifacts)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc


def run_mutation_generation(
    config: ExperimentConfig,
    *,
    config_path: Path,
    artifacts: ExperimentArtifacts,
) -> MutationSummary:
    """Execute or resume mutation generation from normalized examples."""

    LOGGER.info(
        "Mutation stage start normalized_examples=%s mutants=%s resume=%s overwrite=%s",
        artifacts.normalized_examples,
        artifacts.mutants,
        config.resume,
        config.overwrite,
    )
    if not artifacts.normalized_examples.exists():
        raise PipelineError(
            "Mutation generation requires normalized_examples.jsonl, "
            "but it does not exist."
        )

    if artifacts.mutants.exists():
        if config.overwrite:
            pass
        elif config.resume:
            return _summarize_existing_mutations(config, artifacts)
        raise PipelineError(
            "Mutation outputs already exist: mutants.jsonl. "
            "Set resume: true to reuse them or overwrite: true to replace them."
        )

    try:
        examples = read_normalized_examples(artifacts.normalized_examples)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc

    if config.overwrite or not artifacts.config_snapshot.exists():
        _write_config_snapshot(config_path, artifacts.config_snapshot)

    records = generate_mutations(
        examples,
        profile=config.mutation_profile,
    )
    write_mutation_records(artifacts.mutants, records)

    skipped_stages = [
        stage
        for stage in config.stages.enabled()
        if stage not in {"normalize", "mutate"}
    ]
    summary = summarize_mutations(
        records,
        benchmark=config.benchmark,
        profile=config.mutation_profile,
        output_path=artifacts.mutants,
        skipped_stages=skipped_stages,
    )
    LOGGER.info(
        "Mutation stage complete generated=%s skipped=%s output=%s",
        summary.generated_count,
        summary.skipped_count,
        summary.output_path,
    )
    return summary


def run_normalization(
    config: ExperimentConfig,
    *,
    config_path: Path,
    artifacts: ExperimentArtifacts,
) -> NormalizationSummary:
    """Execute the implemented normalization stage for the configured benchmark."""

    LOGGER.info(
        "Normalization stage start benchmark=%s mode=%s data_root=%s output=%s",
        config.benchmark,
        config.bird_mode,
        config.data_root,
        artifacts.normalized_examples,
    )
    try:
        examples = normalize_examples(config)
    except BenchmarkError as exc:
        raise PipelineError(str(exc)) from exc

    mode = config.bird_mode
    if mode is None:
        raise PipelineError("bird_mode is required for benchmark 'bird'.")

    existing_outputs = [
        path
        for path in (artifacts.config_snapshot, artifacts.normalized_examples)
        if path.exists()
    ]
    if existing_outputs:
        if config.overwrite:
            pass
        elif config.resume:
            return _summarize_existing_normalization(config, artifacts, mode)
        existing = ", ".join(str(path.name) for path in existing_outputs)
        raise PipelineError(
            f"Normalization outputs already exist: {existing}. "
            "Set resume: true to reuse them or overwrite: true to replace them."
        )

    _write_config_snapshot(config_path, artifacts.config_snapshot)
    _write_examples(artifacts.normalized_examples, examples)

    skipped_stages = [
        stage for stage in config.stages.enabled() if stage != "normalize"
    ]
    summary = NormalizationSummary(
        benchmark=config.benchmark,
        mode=mode,
        example_count=len(examples),
        database_count=len({example.db_id for example in examples}),
        output_path=str(artifacts.normalized_examples),
        skipped_stages=skipped_stages,
    )
    LOGGER.info(
        "Normalization stage complete examples=%s databases=%s output=%s",
        summary.example_count,
        summary.database_count,
        summary.output_path,
    )
    return summary


def _write_config_snapshot(source: Path, target: Path) -> None:
    """Copy the user-supplied config into the experiment artifact directory."""

    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _write_examples(path: Path, examples: list[NormalizedExample]) -> None:
    """Write normalized examples as JSONL in source order."""

    with path.open("w", encoding="utf-8") as file:
        for example in examples:
            file.write(json.dumps(example.to_dict(), ensure_ascii=True))
            file.write("\n")


def _summarize_existing_normalization(
    config: ExperimentConfig,
    artifacts: ExperimentArtifacts,
    mode: str,
) -> NormalizationSummary:
    """Summarize a previously written normalization artifact for resume mode."""

    if not artifacts.normalized_examples.exists():
        raise PipelineError(
            "resume is true but normalized_examples.jsonl does not exist."
        )

    example_count = 0
    db_ids: set[str] = set()
    with artifacts.normalized_examples.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PipelineError(
                    "Existing normalized_examples.jsonl is not valid JSONL: "
                    f"line {line_number}."
                ) from exc
            if not isinstance(row, dict) or not isinstance(row.get("db_id"), str):
                raise PipelineError(
                    "Existing normalized_examples.jsonl has an invalid row shape: "
                    f"line {line_number}."
                )
            example_count += 1
            db_ids.add(row["db_id"])

    skipped_stages = [
        stage for stage in config.stages.enabled() if stage != "normalize"
    ]
    return NormalizationSummary(
        benchmark=config.benchmark,
        mode=mode,
        example_count=example_count,
        database_count=len(db_ids),
        output_path=str(artifacts.normalized_examples),
        resumed=True,
        skipped_stages=skipped_stages,
    )


def _summarize_existing_mutations(
    config: ExperimentConfig,
    artifacts: ExperimentArtifacts,
) -> MutationSummary:
    """Summarize an existing mutation artifact when resume mode is enabled."""

    try:
        records = read_mutation_records(artifacts.mutants)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc

    skipped_stages = [
        stage
        for stage in config.stages.enabled()
        if stage not in {"normalize", "mutate"}
    ]
    return summarize_mutations(
        records,
        benchmark=config.benchmark,
        profile=config.mutation_profile,
        output_path=artifacts.mutants,
        resumed=True,
        skipped_stages=skipped_stages,
    )
