"""VeriEQL-backed equivalence filtering."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

from sqlmt.artifacts import ExperimentArtifacts
from sqlmt.config import ExperimentConfig
from sqlmt.models import (
    EquivalenceOutcome,
    EquivalenceRecord,
    EquivalenceSummary,
    MutationRecord,
    NormalizedExample,
    NormalizedSchema,
)
from sqlmt.mutation.generator import read_mutation_records, read_normalized_examples


class VeriEQLRunner(Protocol):
    """Callable used to execute the VeriEQL subprocess."""

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...

_VERIEQL_TYPE_MAP = {
    "boolean": "BOOL",
    "bool": "BOOL",
    "integer": "INT",
    "int": "INT",
    "number": "REAL",
    "real": "REAL",
    "float": "REAL",
    "double": "REAL",
    "decimal": "REAL",
    "numeric": "REAL",
    "date": "DATE",
    "datetime": "DATE",
    "time": "DATE",
    "timestamp": "DATE",
    "text": "VARCHAR",
    "varchar": "VARCHAR",
    "string": "VARCHAR",
}


def build_verieql_input_rows(
    mutations: list[MutationRecord],
    examples: list[NormalizedExample],
) -> list[dict[str, object]]:
    """Build VeriEQL JSONL rows for generated mutants only."""

    examples_by_id = {example.example_id: example for example in examples}
    rows: list[dict[str, object]] = []
    for mutation in mutations:
        if mutation.status != "generated" or mutation.mutant_sql is None:
            continue
        example = examples_by_id.get(mutation.example_id)
        if example is None:
            raise ValueError(
                "No normalized example found for generated mutant "
                f"{mutation.mutant_id}: {mutation.example_id}."
            )
        rows.append(
            {
                "index": len(rows),
                "file": mutation.mutant_id,
                "schema": _schema_for_verieql(example.schema),
                "constraint": _constraints_for_verieql(example.schema),
                "pair": [mutation.gold_sql, mutation.mutant_sql],
            }
        )
    return rows


def run_equivalence_filtering(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
    runner: VeriEQLRunner | None = None,
) -> EquivalenceSummary:
    """Run VeriEQL over generated mutants and persist explicit outcomes."""

    if artifacts.equivalence.exists():
        if config.overwrite:
            pass
        elif config.resume:
            return summarize_equivalence(
                read_equivalence_records(artifacts.equivalence),
                output_path=artifacts.equivalence,
                resumed=True,
                skipped_stages=_skipped_stages(config),
            )
        raise ValueError(
            "Equivalence outputs already exist: equivalence.jsonl. "
            "Set resume: true to reuse them or overwrite: true to replace them."
        )

    if config.tools.verieql_path is None:
        raise ValueError("tools.verieql_path is required when equivalence is enabled.")
    if config.tools.verieql_python is None:
        raise ValueError(
            "tools.verieql_python is required when equivalence is enabled."
        )
    if not artifacts.normalized_examples.exists():
        raise ValueError(
            "Equivalence filtering requires normalized_examples.jsonl, "
            "but it does not exist."
        )
    if not artifacts.mutants.exists():
        raise ValueError(
            "Equivalence filtering requires mutants.jsonl, but it does not exist."
        )

    examples = read_normalized_examples(artifacts.normalized_examples)
    mutations = read_mutation_records(artifacts.mutants)
    input_rows = build_verieql_input_rows(mutations, examples)
    generated_mutations = [
        mutation
        for mutation in mutations
        if mutation.status == "generated" and mutation.mutant_sql is not None
    ]

    with tempfile.TemporaryDirectory(
        prefix="verieql-", dir=artifacts.root
    ) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        input_path = temp_dir / "input.jsonlines"
        output_path = temp_dir / "output.out"
        _write_jsonl(input_path, input_rows)
        command = [
            str(config.tools.verieql_python),
            "-m",
            "parallel.cli_within_timeout",
            "-f",
            str(input_path),
            "-t",
            str(config.timeouts.verieql_seconds),
            "-c",
            str(config.workers),
            "-o",
            str(output_path),
        ]
        active_runner = runner or _run_subprocess
        try:
            _invoke_runner(
                active_runner,
                command,
                cwd=config.tools.verieql_path,
                timeout=_subprocess_timeout(
                    config.timeouts.verieql_seconds,
                    len(generated_mutations),
                ),
            )
        except subprocess.TimeoutExpired as exc:
            records = _timeout_records_for_subprocess(
                generated_mutations,
                timeout_seconds=config.timeouts.verieql_seconds,
                error=str(exc),
            )
        except subprocess.CalledProcessError as exc:
            records = _error_records_for_subprocess(
                generated_mutations,
                timeout_seconds=config.timeouts.verieql_seconds,
                error=exc.stderr or exc.stdout or str(exc),
            )
        else:
            records = _records_from_verieql_output(
                output_path,
                generated_mutations,
                timeout_seconds=config.timeouts.verieql_seconds,
            )

    write_equivalence_records(artifacts.equivalence, records)
    return summarize_equivalence(
        records,
        output_path=artifacts.equivalence,
        skipped_stages=_skipped_stages(config),
    )


def read_equivalence_records(path: Path) -> list[EquivalenceRecord]:
    """Read equivalence records from a JSONL artifact."""

    rows: list[EquivalenceRecord] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"equivalence.jsonl is not valid JSONL: line {line_number}."
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(
                    f"equivalence.jsonl row {line_number} must be a JSON object."
                )
            rows.append(EquivalenceRecord.from_dict(row))
    return rows


def write_equivalence_records(
    path: Path, records: list[EquivalenceRecord]
) -> None:
    """Write equivalence records as JSONL."""

    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record.to_dict(), ensure_ascii=True))
            file.write("\n")


def summarize_equivalence(
    records: list[EquivalenceRecord],
    *,
    output_path: Path,
    resumed: bool = False,
    skipped_stages: list[str] | None = None,
) -> EquivalenceSummary:
    """Aggregate equivalence outcome counts."""

    return EquivalenceSummary(
        checked_count=len(records),
        proven_equivalent_count=_count(records, "proven_equivalent"),
        proven_non_equivalent_count=_count(records, "proven_non_equivalent"),
        timeout_count=_count(records, "timeout"),
        unsupported_count=_count(records, "unsupported"),
        error_count=_count(records, "error"),
        output_path=str(output_path),
        resumed=resumed,
        skipped_stages=list(skipped_stages or []),
    )


def _run_subprocess(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run VeriEQL and raise when the process fails."""

    return subprocess.run(
        command,
        cwd=cwd,
        timeout=timeout,
        check=True,
        capture_output=True,
        text=True,
    )


def _invoke_runner(
    runner: VeriEQLRunner,
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Call a test or production runner with keyword-only context."""

    return runner(command, cwd=cwd, timeout=timeout)


def _records_from_verieql_output(
    output_path: Path,
    mutations: list[MutationRecord],
    *,
    timeout_seconds: int,
) -> list[EquivalenceRecord]:
    """Parse VeriEQL output rows into stable project records."""

    rows_by_file = _read_raw_verieql_rows(output_path)
    records: list[EquivalenceRecord] = []
    for mutation in mutations:
        row = rows_by_file.get(mutation.mutant_id)
        if row is None:
            records.append(
                _error_record(
                    mutation,
                    timeout_seconds=timeout_seconds,
                    error="VeriEQL did not write a result row for this mutant.",
                )
            )
            continue
        states = row.get("states", [])
        raw_state = _last_state(states)
        records.append(
            EquivalenceRecord(
                mutant_id=mutation.mutant_id,
                example_id=mutation.example_id,
                outcome=_outcome_for_state(raw_state),
                raw_state=raw_state,
                timeout_seconds=timeout_seconds,
                verieql_index=(
                    int(row["index"]) if row.get("index") is not None else None
                ),
                times=list(row.get("times", [])),
                counterexample_sql=(
                    str(row["counterexample"])
                    if row.get("counterexample") is not None
                    else None
                ),
                error=str(row["err"]) if row.get("err") is not None else None,
            )
        )
    return records


def _read_raw_verieql_rows(path: Path) -> dict[str, dict[str, Any]]:
    """Read raw VeriEQL output keyed by the input ``file`` field."""

    if not path.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                rows[f"__malformed_{line_number}"] = {
                    "file": f"__malformed_{line_number}",
                    "states": ["OTE"],
                    "err": f"Malformed VeriEQL output at line {line_number}.",
                }
                continue
            if isinstance(row, dict) and isinstance(row.get("file"), str):
                rows[str(row["file"])] = row
    return rows


def _error_records_for_subprocess(
    mutations: list[MutationRecord],
    *,
    timeout_seconds: int,
    error: str,
) -> list[EquivalenceRecord]:
    """Build one error record per generated mutant after process failure."""

    return [
        _error_record(
            mutation,
            timeout_seconds=timeout_seconds,
            error=error,
        )
        for mutation in mutations
    ]


def _timeout_records_for_subprocess(
    mutations: list[MutationRecord],
    *,
    timeout_seconds: int,
    error: str,
) -> list[EquivalenceRecord]:
    """Build one timeout record per mutant after batch timeout."""

    return [
        EquivalenceRecord(
            mutant_id=mutation.mutant_id,
            example_id=mutation.example_id,
            outcome="timeout",
            raw_state="TMO",
            timeout_seconds=timeout_seconds,
            error=error,
        )
        for mutation in mutations
    ]


def _error_record(
    mutation: MutationRecord,
    *,
    timeout_seconds: int,
    error: str,
) -> EquivalenceRecord:
    """Build a stable error row for a mutant."""

    return EquivalenceRecord(
        mutant_id=mutation.mutant_id,
        example_id=mutation.example_id,
        outcome="error",
        raw_state=None,
        timeout_seconds=timeout_seconds,
        error=error,
    )


def _schema_for_verieql(schema: NormalizedSchema) -> dict[str, dict[str, str]]:
    """Convert normalized schema metadata into VeriEQL's table/column map."""

    result: dict[str, dict[str, str]] = {}
    for table_names in (schema.table_names_original, schema.table_names):
        for table_name in table_names:
            result.setdefault(_verieql_identifier(table_name), {})

    for index, (table_index, column_name) in enumerate(schema.column_names_original):
        if table_index < 0 or column_name == "*":
            continue
        column_type = (
            schema.column_types[index] if index < len(schema.column_types) else ""
        )
        for table_names in (schema.table_names_original, schema.table_names):
            if table_index >= len(table_names):
                continue
            table_name = _verieql_identifier(table_names[table_index])
            result[table_name][_verieql_identifier(column_name)] = _verieql_type(
                column_type
            )
    return result


def _constraints_for_verieql(schema: NormalizedSchema) -> list[dict[str, object]]:
    """Convert primary and foreign keys to VeriEQL constraint rows."""

    constraints: list[dict[str, object]] = []
    for primary_key in schema.primary_keys:
        key_indexes = primary_key if isinstance(primary_key, tuple) else (primary_key,)
        values = [
            value
            for index in key_indexes
            if (value := _constraint_value(schema, index)) is not None
        ]
        if values:
            constraints.append({"primary": [{"value": value} for value in values]})
    for source, target in schema.foreign_keys:
        source_value = _constraint_value(schema, source)
        target_value = _constraint_value(schema, target)
        if source_value is None or target_value is None:
            continue
        constraints.append(
            {
                "foreign": [
                    {"value": source_value},
                    {"value": target_value},
                ]
            }
        )
    return constraints


def _constraint_value(schema: NormalizedSchema, column_index: int) -> str | None:
    """Return VeriEQL's ``table__column`` constraint reference."""

    if column_index < 0 or column_index >= len(schema.column_names_original):
        return None
    table_index, column_name = schema.column_names_original[column_index]
    table_names = schema.table_names_original or schema.table_names
    if table_index < 0 or table_index >= len(table_names):
        return None
    table_name = _verieql_identifier(table_names[table_index])
    column = _verieql_identifier(column_name)
    return f"{table_name}__{column}"


def _verieql_identifier(identifier: str) -> str:
    """Normalize unquoted benchmark identifiers to VeriEQL's parser casing."""

    return identifier.upper()


def _verieql_type(column_type: str) -> str:
    """Map benchmark column types to VeriEQL's coarse SQL type names."""

    return _VERIEQL_TYPE_MAP.get(column_type.lower(), "VARCHAR")


def _outcome_for_state(raw_state: str | None) -> EquivalenceOutcome:
    """Map VeriEQL's compact state to the project's explicit outcome."""

    if raw_state == "EQU":
        return "proven_equivalent"
    if raw_state == "NEQ":
        return "proven_non_equivalent"
    if raw_state == "TMO":
        return "timeout"
    if raw_state in {"NSE", "NIE"}:
        return "unsupported"
    return "error"


def _last_state(states: object) -> str | None:
    """Return the final VeriEQL state from a raw state list."""

    if not isinstance(states, list) or not states:
        return None
    return str(states[-1])


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """Write dictionaries as JSONL."""

    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=True))
            file.write("\n")


def _count(records: list[EquivalenceRecord], outcome: EquivalenceOutcome) -> int:
    """Count records with one outcome."""

    return sum(1 for record in records if record.outcome == outcome)


def _subprocess_timeout(pair_timeout_seconds: int, pair_count: int) -> int:
    """Return a conservative timeout for one VeriEQL batch subprocess."""

    return pair_timeout_seconds * max(1, pair_count) + 60


def _skipped_stages(config: ExperimentConfig) -> list[str]:
    """Return enabled stages after equivalence that remain unimplemented."""

    return [
        stage
        for stage in config.stages.enabled()
        if stage not in {"normalize", "mutate", "equivalence"}
    ]
