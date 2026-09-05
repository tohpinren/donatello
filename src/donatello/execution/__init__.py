"""SQLite execution and mutation scoring."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from donatello.artifacts import ExperimentArtifacts
from donatello.config import ExperimentConfig
from donatello.equivalence import read_equivalence_records
from donatello.models import (
    ExecutionOutcome,
    ExecutionRecord,
    ExecutionSummary,
    MutationRecord,
)
from donatello.mutation.generator import read_mutation_records
from donatello.parallel import ordered_parallel_map
from donatello.result_comparison import ResultSemantics, results_equal
from donatello.sqlite_paths import resolve_sqlite_path

_SAMPLE_LIMIT = 5
LOGGER = logging.getLogger(__name__)


def run_sqlite_execution(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
) -> ExecutionSummary:
    """Run retained non-equivalent mutants against SQLite databases."""

    LOGGER.info(
        "Execution stage start mutants=%s equivalence=%s output=%s "
        "resume=%s overwrite=%s sqlite_timeout=%s workers=%s result_semantics=%s",
        artifacts.mutants,
        artifacts.equivalence,
        artifacts.execution,
        config.resume,
        config.overwrite,
        config.timeouts.sqlite_seconds,
        config.workers,
        config.result_semantics,
    )
    if artifacts.execution.exists():
        if config.resume:
            existing_records = read_execution_records(artifacts.execution)
            _validate_resumed_result_semantics(
                existing_records,
                expected=config.result_semantics,
            )
            resumed = True
        if not config.overwrite:
            if not config.resume:
                raise ValueError(
                    "Execution outputs already exist: execution.jsonl. "
                    "Set resume: true to reuse them or overwrite: true to replace them."
                )
        if config.overwrite:
            artifacts.execution.unlink()
            existing_records = []
            resumed = False
    else:
        existing_records = []
        resumed = False

    for artifact_path, missing_message in (
        (
            artifacts.mutants,
            "SQLite execution requires mutants.jsonl, but it does not exist.",
        ),
        (
            artifacts.equivalence,
            "SQLite execution requires equivalence.jsonl, but it does not exist.",
        ),
    ):
        early = _missing_dependency_outcome(
            artifact_path=artifact_path,
            resumed=resumed,
            existing_records=existing_records,
            output_path=artifacts.execution,
            config=config,
            missing_message=missing_message,
        )
        if early is not None:
            return early

    mutations = read_mutation_records(artifacts.mutants)
    equivalence_records = read_equivalence_records(artifacts.equivalence)
    retained = _retained_mutations(mutations, equivalence_records)
    existing_ids = {record.mutant_id for record in existing_records}
    pending = [
        mutation for mutation in retained if mutation.mutant_id not in existing_ids
    ]
    LOGGER.info(
        "Execution selection retained=%s existing=%s pending=%s",
        len(retained),
        len(existing_records),
        len(pending),
    )
    _ensure_execution_artifact(artifacts.execution)
    new_records = execute_mutations(
        pending,
        timeout_seconds=config.timeouts.sqlite_seconds,
        workers=config.workers,
        output_path=artifacts.execution,
        data_root=config.data_root,
        result_semantics=config.result_semantics,
    )
    records = existing_records + new_records
    summary = summarize_execution(
        records,
        output_path=artifacts.execution,
        resumed=resumed,
        skipped_stages=_skipped_stages(config),
    )
    LOGGER.info(
        "Execution stage complete executed=%s killed=%s survived=%s "
        "timeout=%s error=%s score=%s output=%s",
        summary.executed_count,
        summary.killed_count,
        summary.survived_count,
        summary.timeout_count,
        summary.error_count,
        summary.mutation_score,
        summary.output_path,
    )
    return summary


def execute_mutations(
    mutations: list[MutationRecord],
    *,
    timeout_seconds: int = 120,
    workers: int = 1,
    output_path: Path | None = None,
    data_root: Path | None = None,
    result_semantics: ResultSemantics = "bag",
) -> list[ExecutionRecord]:
    """Execute retained mutants and compare them with cached gold SQL results."""

    records: list[ExecutionRecord] = []
    gold_cache = _GoldQueryCache(
        timeout_seconds=timeout_seconds,
        data_root=data_root,
    )
    LOGGER.info(
        "Executing sqlite mutations pending=%s timeout_seconds=%s workers=%s "
        "result_semantics=%s",
        len(mutations),
        timeout_seconds,
        workers,
        result_semantics,
    )
    for record in ordered_parallel_map(
        mutations,
        workers=workers,
        fn=lambda mutation: _execute_mutation(
            mutation,
            timeout_seconds=timeout_seconds,
            gold_cache=gold_cache,
            data_root=data_root,
            result_semantics=result_semantics,
        ),
    ):
        _record_result(records, record, output_path=output_path)
    return records


@dataclass
class _GoldCacheEntry:
    """One shared gold-query result used across worker threads."""

    ready: threading.Event
    value: list[tuple[Any, ...]] | None = None
    error: BaseException | None = None


class _GoldQueryCache:
    """Memoize gold query execution once per database/query pair."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        data_root: Path | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._data_root = data_root
        self._entries: dict[tuple[str, str], _GoldCacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, mutation: MutationRecord) -> list[tuple[Any, ...]]:
        """Return cached gold rows, computing them once when needed."""

        resolved_path = _resolved_sqlite_path(
            mutation,
            data_root=self._data_root,
        )
        key = (str(resolved_path), mutation.gold_sql)
        with self._lock:
            entry = self._entries.get(key)
            owner = entry is None
            if owner:
                entry = _GoldCacheEntry(ready=threading.Event())
                self._entries[key] = entry

        assert entry is not None
        if owner:
            try:
                entry.value = _execute_sql(
                    resolved_path,
                    mutation.gold_sql,
                    timeout_seconds=self._timeout_seconds,
                )
            except BaseException as exc:  # noqa: B036
                entry.error = exc
                LOGGER.warning(
                    "Gold query failed mutant_id=%s db=%s error=%s",
                    mutation.mutant_id,
                    mutation.sqlite_path,
                    exc,
                )
            finally:
                entry.ready.set()
        else:
            entry.ready.wait()

        if entry.error is not None:
            raise entry.error
        assert entry.value is not None
        return entry.value


def _execute_mutation(
    mutation: MutationRecord,
    *,
    timeout_seconds: int,
    gold_cache: _GoldQueryCache,
    data_root: Path | None,
    result_semantics: ResultSemantics,
) -> ExecutionRecord:
    """Execute one mutant against SQLite."""

    assert mutation.mutant_sql is not None
    try:
        gold_rows = gold_cache.get(mutation)
    except Exception as exc:
        LOGGER.warning(
            "Execution error phase=gold mutant_id=%s error=%s",
            mutation.mutant_id,
            exc,
        )
        return _error_record(
            mutation,
            "gold",
            exc,
            result_semantics=result_semantics,
        )

    try:
        sqlite_path = _resolved_sqlite_path(mutation, data_root=data_root)
        mutant_rows = _execute_sql(
            sqlite_path,
            mutation.mutant_sql,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        LOGGER.warning(
            "Execution error phase=mutant mutant_id=%s error=%s",
            mutation.mutant_id,
            exc,
        )
        return _error_record(
            mutation,
            "mutant",
            exc,
            result_semantics=result_semantics,
        )

    outcome: ExecutionOutcome = (
        "survived"
        if results_equal(gold_rows, mutant_rows, semantics=result_semantics)
        else "killed"
    )
    LOGGER.debug(
        "Execution compared mutant_id=%s outcome=%s gold_rows=%s mutant_rows=%s",
        mutation.mutant_id,
        outcome,
        len(gold_rows),
        len(mutant_rows),
    )
    return ExecutionRecord(
        mutant_id=mutation.mutant_id,
        example_id=mutation.example_id,
        outcome=outcome,
        gold_sql=mutation.gold_sql,
        mutant_sql=mutation.mutant_sql,
        sqlite_path=mutation.sqlite_path,
        rule=mutation.rule,
        profile=mutation.profile,
        result_semantics=result_semantics,
        gold_row_count=len(gold_rows),
        mutant_row_count=len(mutant_rows),
        gold_sample=_sample_rows(gold_rows),
        mutant_sample=_sample_rows(mutant_rows),
    )


def read_execution_records(path: Path) -> list[ExecutionRecord]:
    """Read execution records from a JSONL artifact."""

    records: list[ExecutionRecord] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"execution.jsonl is not valid JSONL: line {line_number}."
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(
                    f"execution.jsonl row {line_number} must be a JSON object."
                )
            records.append(ExecutionRecord.from_dict(row))
    return records


def write_execution_records(path: Path, records: list[ExecutionRecord]) -> None:
    """Write execution records as JSONL."""

    with path.open("w", encoding="utf-8") as file:
        for record in records:
            _write_execution_jsonl_line(file, record=record)


def append_execution_record(path: Path, record: ExecutionRecord) -> None:
    """Append one execution record to a JSONL artifact."""

    with path.open("a", encoding="utf-8") as file:
        _write_execution_jsonl_line(file, record=record)


def _write_execution_jsonl_line(file: TextIO, *, record: ExecutionRecord) -> None:
    """Serialize one execution record as a single JSONL line."""

    file.write(json.dumps(record.to_dict(), ensure_ascii=True))
    file.write("\n")


def _record_result(
    records: list[ExecutionRecord],
    record: ExecutionRecord,
    *,
    output_path: Path | None,
) -> None:
    """Keep the in-memory summary and on-disk checkpoint in sync."""

    records.append(record)
    if output_path is not None:
        append_execution_record(output_path, record)


def summarize_execution(
    records: list[ExecutionRecord],
    *,
    output_path: Path,
    resumed: bool = False,
    skipped_stages: list[str] | None = None,
) -> ExecutionSummary:
    """Aggregate execution counts and mutation score."""

    killed_count = _count(records, "killed")
    survived_count = _count(records, "survived")
    denominator = killed_count + survived_count
    return ExecutionSummary(
        executed_count=len(records),
        killed_count=killed_count,
        survived_count=survived_count,
        timeout_count=_count(records, "timeout"),
        error_count=_count(records, "error"),
        mutation_score=(killed_count / denominator if denominator else None),
        output_path=str(output_path),
        resumed=resumed,
        skipped_stages=list(skipped_stages or []),
    )


def _missing_dependency_outcome(
    *,
    artifact_path: Path,
    resumed: bool,
    existing_records: list[ExecutionRecord],
    output_path: Path,
    config: ExperimentConfig,
    missing_message: str,
) -> ExecutionSummary | None:
    """If a required artifact is absent, return a resume summary or raise."""

    if artifact_path.exists():
        return None
    if resumed:
        return summarize_execution(
            existing_records,
            output_path=output_path,
            resumed=True,
            skipped_stages=_skipped_stages(config),
        )
    raise ValueError(missing_message)


def _retained_mutations(
    mutations: list[MutationRecord],
    equivalence_records: list[Any],
) -> list[MutationRecord]:
    """Return generated mutants proven non-equivalent by VeriEQL."""

    retained_ids = {
        record.mutant_id
        for record in equivalence_records
        if record.outcome == "proven_non_equivalent"
    }
    return [
        mutation
        for mutation in mutations
        if mutation.status == "generated"
        and mutation.mutant_sql is not None
        and mutation.mutant_id in retained_ids
    ]


def _ensure_execution_artifact(path: Path) -> None:
    """Create an empty execution artifact so empty runs are resumable."""

    if not path.exists():
        path.touch()


def _resolved_sqlite_path(
    mutation: MutationRecord,
    *,
    data_root: Path | None,
) -> Path:
    """Return the executable SQLite path for one mutation in the current env."""

    if data_root is None:
        return Path(mutation.sqlite_path)
    return resolve_sqlite_path(
        sqlite_path=mutation.sqlite_path,
        sqlite_relpath=mutation.sqlite_relpath,
        data_root=data_root,
    )


def _execute_sql(
    path: Path,
    sql: str,
    *,
    timeout_seconds: int,
) -> list[tuple[Any, ...]]:
    """Execute one SQL query against an existing SQLite database."""

    if not path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")

    deadline = time.monotonic() + timeout_seconds
    # Some large benchmark SQLite files fail to open reliably from a Docker
    # bind mount unless we opt out of lock/file-change checks for read-only use.
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.set_progress_handler(
        lambda: 1 if time.monotonic() >= deadline else 0,
        1000,
    )
    try:
        try:
            cursor = connection.execute(sql)
            return [tuple(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError as exc:
            if str(exc) == "interrupted" and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"SQLite query timed out after {timeout_seconds} seconds."
                ) from exc
            raise
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()


def _error_record(
    mutation: MutationRecord,
    phase: str,
    error: Exception,
    *,
    result_semantics: ResultSemantics,
) -> ExecutionRecord:
    """Build an execution error record for one mutant."""

    outcome: ExecutionOutcome = (
        "timeout" if isinstance(error, TimeoutError) else "error"
    )
    return ExecutionRecord(
        mutant_id=mutation.mutant_id,
        example_id=mutation.example_id,
        outcome=outcome,
        gold_sql=mutation.gold_sql,
        mutant_sql=mutation.mutant_sql or "",
        sqlite_path=mutation.sqlite_path,
        rule=mutation.rule,
        profile=mutation.profile,
        result_semantics=result_semantics,
        error_phase=phase,
        error=str(error),
    )


def _sample_rows(rows: list[tuple[Any, ...]]) -> list[list[Any]]:
    """Return a JSON-safe sample of result rows."""

    return [[_serialize_value(value) for value in row] for row in rows[:_SAMPLE_LIMIT]]


def _serialize_value(value: Any) -> Any:
    """Convert SQLite scalar values into JSON-safe artifact values."""

    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def _count(records: list[ExecutionRecord], outcome: ExecutionOutcome) -> int:
    """Count records with one execution outcome."""

    return sum(1 for record in records if record.outcome == outcome)


def _validate_resumed_result_semantics(
    records: list[ExecutionRecord],
    *,
    expected: ResultSemantics,
) -> None:
    """Prevent resumed execution artifacts from mixing comparison policies."""

    recorded = {record.result_semantics for record in records}
    if not recorded:
        return
    if None in recorded:
        raise ValueError(
            "Existing execution.jsonl does not record result semantics. "
            "Rerun execution with overwrite: true."
        )
    if recorded != {expected}:
        values = ", ".join(sorted(str(value) for value in recorded))
        raise ValueError(
            "Existing execution.jsonl uses result semantics "
            f"{values}; config requests {expected}. Use a separate output_dir "
            "or rerun execution with overwrite: true."
        )


def _skipped_stages(config: ExperimentConfig) -> list[str]:
    """Return enabled stages after execution that remain unimplemented."""

    return [
        stage
        for stage in config.stages.enabled()
        if stage not in {"normalize", "mutate", "equivalence", "execute"}
    ]
