"""Pipeline orchestration for dry runs and benchmark normalization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlmt.artifacts import ExperimentArtifacts, artifacts_for
from sqlmt.benchmarks import BenchmarkError, normalize_examples
from sqlmt.config import ExperimentConfig
from sqlmt.models import NormalizationSummary, NormalizedExample


class PipelineError(RuntimeError):
    """Raised when the configured pipeline cannot be executed."""


@dataclass(frozen=True)
class DryRunPlan:
    experiment_name: str
    output_dir: str
    enabled_stages: list[str]
    artifacts: dict[str, str]


def build_dry_run_plan(config: ExperimentConfig) -> DryRunPlan:
    artifacts = artifacts_for(config.output_dir)
    return DryRunPlan(
        experiment_name=config.experiment_name,
        output_dir=str(artifacts.root),
        enabled_stages=config.stages.enabled(),
        artifacts=artifacts.as_display_dict(),
    )


def prepare_run(config: ExperimentConfig) -> ExperimentArtifacts:
    artifacts = artifacts_for(config.output_dir)
    artifacts.ensure_base_dirs()
    return artifacts


def run_normalization(
    config: ExperimentConfig,
    *,
    config_path: Path,
    artifacts: ExperimentArtifacts,
) -> NormalizationSummary:
    """Execute the implemented normalization stage for the configured benchmark."""

    if not config.stages.normalize:
        raise PipelineError(
            "No implemented stage is enabled. Set stages.normalize: true."
        )

    try:
        examples = normalize_examples(config)
    except BenchmarkError as exc:
        raise PipelineError(str(exc)) from exc

    _write_config_snapshot(config_path, artifacts.config_snapshot)
    _write_examples(artifacts.normalized_examples, examples)

    skipped_stages = [
        stage for stage in config.stages.enabled() if stage != "normalize"
    ]
    return NormalizationSummary(
        benchmark=config.benchmark,
        mode=config.bird_mode,
        example_count=len(examples),
        database_count=len({example.db_id for example in examples}),
        output_path=str(artifacts.normalized_examples),
        skipped_stages=skipped_stages,
    )


def _write_config_snapshot(source: Path, target: Path) -> None:
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _write_examples(path: Path, examples: list[NormalizedExample]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for example in examples:
            file.write(json.dumps(example.to_dict(), ensure_ascii=True))
            file.write("\n")
