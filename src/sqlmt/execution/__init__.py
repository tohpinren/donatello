"""SQLite execution and mutation scoring."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sqlmt.artifacts import ExperimentArtifacts
from sqlmt.config import ExperimentConfig
from sqlmt.equivalence import read_equivalence_records
from sqlmt.models import (
    ExecutionOutcome,
    ExecutionRecord,
    ExecutionSummary,
    MutationRecord,
)
from sqlmt.mutation.generator import read_mutation_records

_SAMPLE_LIMIT = 5


def run_sqlite_execution(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
) -> ExecutionSummary:
    """Run retained non-equivalent mutants against SQLite databases."""

    if artifacts.execution.exists():
        if config.resume:
            return summarize_execution(
                read_execution_records(artifacts.execution),
                output_path=artifacts.execution,
                resumed=True,
                skipped_stages=_skipped_stages(config),
            )
        if not config.overwrite:
            raise ValueError(
                "Execution outputs already exist: execution.jsonl. "
                "Set resume: true to reuse them or overwrite: true to replace them."
            )
        artifacts.execution.unlink()

    if not artifacts.mutants.exists():
        raise ValueError(
            "SQLite execution requires mutants.jsonl, but it does not exist."
        )
    if not artifacts.equivalence.exists():
        raise ValueError(
            "SQLite execution requires equivalence.jsonl, but it does not exist."
        )

    mutations = read_mutation_records(artifacts.mutants)
    equivalence_records = read_equivalence_records(artifacts.equivalence)
    retained = _retained_mutations(mutations, equivalence_records)
    records = execute_mutations(retained)
    write_execution_records(artifacts.execution, records)
    return summarize_execution(
        records,
        output_path=artifacts.execution,
        skipped_stages=_skipped_stages(config),
    )

def execute_mutations(mutations: list[MutationRecord]) -> list[ExecutionRecord]:
    """Execute retained mutants and compare them with cached gold SQL results."""

    records: list[ExecutionRecord] = []
    gold_cache: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for mutation in mutations:
        if mutation.mutant_sql is None:
            continue
        cache_key = (mutation.sqlite_path, mutation.gold_sql)
        try:
            if cache_key not in gold_cache:
                gold_cache[cache_key] = _execute_sql(
                    Path(mutation.sqlite_path),
                    mutation.gold_sql,
                )
            gold_rows = gold_cache[cache_key]
        except Exception as exc:
            records.append(_error_record(mutation, "gold", exc))
            continue

        try:
            mutant_rows = _execute_sql(Path(mutation.sqlite_path), mutation.mutant_sql)
        except Exception as exc:
            records.append(_error_record(mutation, "mutant", exc))
            continue

        outcome: ExecutionOutcome = "survived" if gold_rows == mutant_rows else "killed"
        records.append(
            ExecutionRecord(
                mutant_id=mutation.mutant_id,
                example_id=mutation.example_id,
                outcome=outcome,
                gold_sql=mutation.gold_sql,
                mutant_sql=mutation.mutant_sql,
                sqlite_path=mutation.sqlite_path,
                rule=mutation.rule,
                profile=mutation.profile,
                gold_row_count=len(gold_rows),
                mutant_row_count=len(mutant_rows),
                gold_sample=_sample_rows(gold_rows),
                mutant_sample=_sample_rows(mutant_rows),
            )
        )
    return records


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
            file.write(json.dumps(record.to_dict(), ensure_ascii=True))
            file.write("\n")


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
        error_count=_count(records, "error"),
        mutation_score=(killed_count / denominator if denominator else None),
        output_path=str(output_path),
        resumed=resumed,
        skipped_stages=list(skipped_stages or []),
    )


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


def _execute_sql(path: Path, sql: str) -> list[tuple[Any, ...]]:
    """Execute one SQL query against an existing SQLite database."""

    if not path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cursor = connection.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _error_record(
    mutation: MutationRecord,
    phase: str,
    error: Exception,
) -> ExecutionRecord:
    """Build an execution error record for one mutant."""

    return ExecutionRecord(
        mutant_id=mutation.mutant_id,
        example_id=mutation.example_id,
        outcome="error",
        gold_sql=mutation.gold_sql,
        mutant_sql=mutation.mutant_sql or "",
        sqlite_path=mutation.sqlite_path,
        rule=mutation.rule,
        profile=mutation.profile,
        error_phase=phase,
        error=str(error),
    )


def _sample_rows(rows: list[tuple[Any, ...]]) -> list[list[Any]]:
    """Return a JSON-safe sample of result rows."""

    return [
        [_serialize_value(value) for value in row]
        for row in rows[:_SAMPLE_LIMIT]
    ]


def _serialize_value(value: Any) -> Any:
    """Convert SQLite scalar values into JSON-safe artifact values."""

    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def _count(records: list[ExecutionRecord], outcome: ExecutionOutcome) -> int:
    """Count records with one execution outcome."""

    return sum(1 for record in records if record.outcome == outcome)


def _skipped_stages(config: ExperimentConfig) -> list[str]:
    """Return enabled stages after execution that remain unimplemented."""

    return [
        stage
        for stage in config.stages.enabled()
        if stage not in {"normalize", "mutate", "equivalence", "execute"}
    ]
