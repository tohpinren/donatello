"""SQLDriller-backed counterexample generation."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol, TextIO

from sqlmt.artifacts import ExperimentArtifacts
from sqlmt.config import ExperimentConfig
from sqlmt.equivalence import read_equivalence_records
from sqlmt.execution import read_execution_records
from sqlmt.models import (
    CounterexampleOutcome,
    CounterexampleRecord,
    CounterexampleSummary,
    MutationRecord,
)
from sqlmt.mutation.generator import read_mutation_records
from sqlmt.parallel import ordered_parallel_map


class SQLDrillerRunner(Protocol):
    """Callable used to execute the SQLDriller adapter subprocess."""

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


LOGGER = logging.getLogger(__name__)


def run_counterexample_generation(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
    runner: SQLDrillerRunner | None = None,
) -> CounterexampleSummary:
    """Generate counterexamples for surviving proven non-equivalent mutants."""

    LOGGER.info(
        "Counterexample stage start mutants=%s equivalence=%s execution=%s "
        "output=%s resume=%s overwrite=%s sqldriller_timeout=%s workers=%s",
        artifacts.mutants,
        artifacts.equivalence,
        artifacts.execution,
        artifacts.counterexamples,
        config.resume,
        config.overwrite,
        config.timeouts.sqldriller_seconds,
        config.workers,
    )
    manifest_exists = artifacts.counterexamples.exists()
    output_dir_exists = artifacts.counterexamples_dir.exists()
    if manifest_exists:
        if config.resume:
            existing_records = read_counterexample_records(artifacts.counterexamples)
            resumed = True
        elif config.overwrite:
            existing_records = []
            resumed = False
            artifacts.counterexamples.unlink()
        else:
            raise ValueError(
                "Counterexample outputs already exist: counterexamples.jsonl. "
                "Set resume: true to reuse them or overwrite: true to replace them."
            )
    else:
        existing_records = []
        resumed = False

    if config.overwrite:
        if artifacts.counterexamples.exists():
            artifacts.counterexamples.unlink()
        if artifacts.counterexamples_dir.exists():
            shutil.rmtree(artifacts.counterexamples_dir)
    elif (
        output_dir_exists
        and any(artifacts.counterexamples_dir.iterdir())
        and not manifest_exists
    ):
        raise ValueError(
            "Counterexample outputs already exist: counterexamples/. "
            "Set overwrite: true to replace them."
        )

    if config.tools.sqldriller_path is None:
        raise ValueError(
            "tools.sqldriller_path is required when counterexamples are enabled."
        )
    if config.tools.sqldriller_python is None:
        raise ValueError(
            "tools.sqldriller_python is required when counterexamples are enabled."
        )
    sqldriller_path = _require_sqldriller_path(config)
    sqldriller_python = _require_sqldriller_python(config)

    for artifact_path, missing_message in (
        (
            artifacts.mutants,
            "Counterexample generation requires mutants.jsonl, but it does not exist.",
        ),
        (
            artifacts.equivalence,
            "Counterexample generation requires equivalence.jsonl, "
            "but it does not exist.",
        ),
        (
            artifacts.execution,
            "Counterexample generation requires execution.jsonl, "
            "but it does not exist.",
        ),
    ):
        early = _missing_dependency_outcome(
            artifact_path=artifact_path,
            resumed=resumed,
            existing_records=existing_records,
            output_path=artifacts.counterexamples,
            output_dir=artifacts.counterexamples_dir,
            config=config,
            missing_message=missing_message,
        )
        if early is not None:
            return early

    mutations = read_mutation_records(artifacts.mutants)
    equivalence_records = read_equivalence_records(artifacts.equivalence)
    execution_records = read_execution_records(artifacts.execution)
    selected = _selected_mutations(mutations, equivalence_records, execution_records)

    artifacts.counterexamples_dir.mkdir(parents=True, exist_ok=True)
    _ensure_counterexample_artifact(artifacts.counterexamples)

    existing_ids = {record.mutant_id for record in existing_records}
    pending = [
        mutation for mutation in selected if mutation.mutant_id not in existing_ids
    ]
    LOGGER.info(
        "Counterexample selection selected=%s existing=%s pending=%s",
        len(selected),
        len(existing_records),
        len(pending),
    )

    new_records: list[CounterexampleRecord] = []
    active_runner = runner or _run_subprocess
    for record in ordered_parallel_map(
        pending,
        workers=config.workers,
        fn=lambda mutation: _generate_counterexample(
            mutation,
            config=config,
            artifacts=artifacts,
            runner=active_runner,
            sqldriller_path=sqldriller_path,
            sqldriller_python=sqldriller_python,
        ),
    ):
        _record_result(
            new_records,
            record,
            output_path=artifacts.counterexamples,
        )

    records = existing_records + new_records
    summary = summarize_counterexamples(
        records,
        output_path=artifacts.counterexamples,
        output_dir=artifacts.counterexamples_dir,
        resumed=resumed,
        skipped_stages=_skipped_stages(config),
    )
    LOGGER.info(
        "Counterexample stage complete selected=%s generated=%s "
        "not_generated=%s timeout=%s error=%s output=%s",
        summary.selected_count,
        summary.generated_count,
        summary.not_generated_count,
        summary.timeout_count,
        summary.error_count,
        summary.output_path,
    )
    return summary


def read_counterexample_records(path: Path) -> list[CounterexampleRecord]:
    """Read counterexample records from a JSONL artifact."""

    records: list[CounterexampleRecord] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"counterexamples.jsonl is not valid JSONL: line {line_number}."
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(
                    f"counterexamples.jsonl row {line_number} must be a JSON object."
                )
            records.append(CounterexampleRecord.from_dict(row))
    return records


def write_counterexample_records(
    path: Path,
    records: list[CounterexampleRecord],
) -> None:
    """Write counterexample records as JSONL."""

    with path.open("w", encoding="utf-8") as file:
        for record in records:
            _write_counterexample_jsonl_line(file, record=record)


def append_counterexample_record(path: Path, record: CounterexampleRecord) -> None:
    """Append one counterexample record to a JSONL artifact."""

    with path.open("a", encoding="utf-8") as file:
        _write_counterexample_jsonl_line(file, record=record)


def summarize_counterexamples(
    records: list[CounterexampleRecord],
    *,
    output_path: Path,
    output_dir: Path,
    resumed: bool = False,
    skipped_stages: list[str] | None = None,
) -> CounterexampleSummary:
    """Aggregate counterexample outcome counts."""

    return CounterexampleSummary(
        selected_count=len(records),
        generated_count=_count(records, "generated"),
        not_generated_count=_count(records, "not_generated"),
        timeout_count=_count(records, "timeout"),
        error_count=_count(records, "error"),
        output_path=str(output_path),
        output_dir=str(output_dir),
        resumed=resumed,
        skipped_stages=list(skipped_stages or []),
    )


def _generate_counterexample(
    mutation: MutationRecord,
    *,
    config: ExperimentConfig,
    artifacts: ExperimentArtifacts,
    runner: SQLDrillerRunner,
    sqldriller_path: Path,
    sqldriller_python: Path,
) -> CounterexampleRecord:
    """Run the SQLDriller adapter for one selected mutant."""

    LOGGER.debug(
        "Generating counterexample mutant_id=%s db_id=%s rule=%s",
        mutation.mutant_id,
        mutation.db_id,
        mutation.rule,
    )
    with tempfile.TemporaryDirectory(
        prefix="sqldriller-",
        dir=artifacts.root,
    ) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        request_path = temp_dir / "request.json"
        result_path = temp_dir / "result.json"
        request_path.write_text(
            json.dumps(
                _request_payload(
                    mutation,
                    sqldriller_root=sqldriller_path,
                    output_dir=artifacts.counterexamples_dir,
                ),
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )

        command = [
            str(sqldriller_python),
            str(_adapter_script_path()),
            "--request-json",
            str(request_path.resolve()),
            "--result-json",
            str(result_path.resolve()),
        ]
        try:
            runner(
                command,
                cwd=sqldriller_path,
                timeout=config.timeouts.sqldriller_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            LOGGER.warning(
                "SQLDriller timeout mutant_id=%s timeout=%s error=%s",
                mutation.mutant_id,
                config.timeouts.sqldriller_seconds,
                exc,
            )
            return _error_record(
                mutation,
                outcome="timeout",
                error=str(exc),
            )
        except subprocess.CalledProcessError as exc:
            LOGGER.warning(
                "SQLDriller failure mutant_id=%s code=%s error=%s",
                mutation.mutant_id,
                exc.returncode,
                exc.stderr or exc.stdout or str(exc),
            )
            return _error_record(
                mutation,
                outcome="error",
                error=exc.stderr or exc.stdout or str(exc),
            )

        return _record_from_result_path(mutation, result_path)


def _record_from_result_path(
    mutation: MutationRecord,
    result_path: Path,
) -> CounterexampleRecord:
    """Parse one adapter JSON result into the stable project record."""

    if not result_path.exists():
        return _error_record(
            mutation,
            outcome="error",
            error="SQLDriller adapter did not write a result file.",
        )
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _error_record(
            mutation,
            outcome="error",
            error=f"SQLDriller adapter wrote invalid JSON: {exc}",
        )
    if not isinstance(payload, dict):
        return _error_record(
            mutation,
            outcome="error",
            error="SQLDriller adapter result must be a JSON object.",
        )

    raw_tag_value = payload.get("raw_tag")
    raw_tag = int(raw_tag_value) if raw_tag_value is not None else None
    counterexample_path = payload.get("counterexample_path")
    if raw_tag == 0:
        if counterexample_path is None:
            return _error_record(
                mutation,
                outcome="error",
                raw_tag=raw_tag,
                error="SQLDriller reported a counterexample without a saved path.",
            )
        return CounterexampleRecord(
            mutant_id=mutation.mutant_id,
            example_id=mutation.example_id,
            outcome="generated",
            gold_sql=mutation.gold_sql,
            mutant_sql=mutation.mutant_sql or "",
            source_sqlite_path=mutation.sqlite_path,
            rule=mutation.rule,
            profile=mutation.profile,
            counterexample_path=str(counterexample_path),
            raw_tag=raw_tag,
            metadata=_record_metadata(mutation),
        )
    if raw_tag in {1, -1}:
        return CounterexampleRecord(
            mutant_id=mutation.mutant_id,
            example_id=mutation.example_id,
            outcome="not_generated",
            gold_sql=mutation.gold_sql,
            mutant_sql=mutation.mutant_sql or "",
            source_sqlite_path=mutation.sqlite_path,
            rule=mutation.rule,
            profile=mutation.profile,
            raw_tag=raw_tag,
            metadata=_record_metadata(mutation),
        )
    return _error_record(
        mutation,
        outcome="error",
        raw_tag=raw_tag,
        error="SQLDriller adapter returned an unknown raw_tag.",
    )


def _request_payload(
    mutation: MutationRecord,
    *,
    sqldriller_root: Path,
    output_dir: Path,
) -> dict[str, object]:
    """Build the JSON payload consumed by the adapter subprocess."""

    return {
        "mutant_id": mutation.mutant_id,
        "db_id": mutation.db_id,
        "benchmark": mutation.benchmark,
        "gold_sql": mutation.gold_sql,
        "mutant_sql": mutation.mutant_sql,
        "source_sqlite_path": str(Path(mutation.sqlite_path).resolve()),
        "sqldriller_root": str(sqldriller_root.resolve()),
        "output_dir": str(output_dir.resolve()),
    }


def _selected_mutations(
    mutations: list[MutationRecord],
    equivalence_records: list[Any],
    execution_records: list[Any],
) -> list[MutationRecord]:
    """Return generated mutants that survived after proven non-equivalence."""

    proven_non_equivalent_ids = {
        record.mutant_id
        for record in equivalence_records
        if record.outcome == "proven_non_equivalent"
    }
    survived_ids = {
        record.mutant_id for record in execution_records if record.outcome == "survived"
    }
    return [
        mutation
        for mutation in mutations
        if mutation.status == "generated"
        and mutation.mutant_sql is not None
        and mutation.mutant_id in proven_non_equivalent_ids
        and mutation.mutant_id in survived_ids
    ]


def _record_metadata(mutation: MutationRecord) -> dict[str, Any]:
    """Capture lightweight context useful for later augmentation stages."""

    return {
        "benchmark": mutation.benchmark,
        "subset": mutation.subset,
        "db_id": mutation.db_id,
        "source_id": mutation.source_id,
        "question": mutation.question,
    }


def _missing_dependency_outcome(
    *,
    artifact_path: Path,
    resumed: bool,
    existing_records: list[CounterexampleRecord],
    output_path: Path,
    output_dir: Path,
    config: ExperimentConfig,
    missing_message: str,
) -> CounterexampleSummary | None:
    """If a required artifact is absent, return a resume summary or raise."""

    if artifact_path.exists():
        return None
    if resumed:
        return summarize_counterexamples(
            existing_records,
            output_path=output_path,
            output_dir=output_dir,
            resumed=True,
            skipped_stages=_skipped_stages(config),
        )
    raise ValueError(missing_message)


def _run_subprocess(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run the SQLDriller adapter and raise when it fails."""

    return subprocess.run(
        command,
        cwd=cwd,
        timeout=timeout,
        check=True,
        capture_output=True,
        text=True,
    )


def _write_counterexample_jsonl_line(
    file: TextIO,
    *,
    record: CounterexampleRecord,
) -> None:
    """Serialize one counterexample record as a single JSONL line."""

    file.write(json.dumps(record.to_dict(), ensure_ascii=True))
    file.write("\n")


def _record_result(
    records: list[CounterexampleRecord],
    record: CounterexampleRecord,
    *,
    output_path: Path | None,
) -> None:
    """Keep the in-memory summary and on-disk checkpoint in sync."""

    records.append(record)
    if output_path is not None:
        append_counterexample_record(output_path, record)


def _ensure_counterexample_artifact(path: Path) -> None:
    """Create an empty manifest so empty runs are resumable."""

    if not path.exists():
        path.touch()


def _adapter_script_path() -> Path:
    """Return the path to the repo-owned SQLDriller adapter script."""

    return Path(__file__).with_name("adapter.py")


def _require_sqldriller_path(config: ExperimentConfig) -> Path:
    """Return the validated SQLDriller checkout path."""

    path = config.tools.sqldriller_path
    if path is None:
        raise ValueError(
            "tools.sqldriller_path is required when counterexamples are enabled."
        )
    return path


def _require_sqldriller_python(config: ExperimentConfig) -> Path:
    """Return the validated SQLDriller Python path."""

    path = config.tools.sqldriller_python
    if path is None:
        raise ValueError(
            "tools.sqldriller_python is required when counterexamples are enabled."
        )
    return path


def _error_record(
    mutation: MutationRecord,
    *,
    outcome: CounterexampleOutcome,
    error: str,
    raw_tag: int | None = None,
) -> CounterexampleRecord:
    """Build a stable timeout/error row for one mutant."""

    return CounterexampleRecord(
        mutant_id=mutation.mutant_id,
        example_id=mutation.example_id,
        outcome=outcome,
        gold_sql=mutation.gold_sql,
        mutant_sql=mutation.mutant_sql or "",
        source_sqlite_path=mutation.sqlite_path,
        rule=mutation.rule,
        profile=mutation.profile,
        raw_tag=raw_tag,
        error=error,
        metadata=_record_metadata(mutation),
    )


def _count(records: list[CounterexampleRecord], outcome: CounterexampleOutcome) -> int:
    """Count records with one counterexample outcome."""

    return sum(1 for record in records if record.outcome == outcome)


def _skipped_stages(config: ExperimentConfig) -> list[str]:
    """Return enabled stages after counterexample generation."""

    return [
        stage
        for stage in config.stages.enabled()
        if stage
        not in {"normalize", "mutate", "equivalence", "execute", "counterexamples"}
    ]
