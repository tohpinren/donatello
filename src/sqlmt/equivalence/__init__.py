"""VeriEQL-backed equivalence filtering."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol, TextIO

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
LOGGER = logging.getLogger(__name__)


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

    LOGGER.info(
        "Equivalence stage start mutants=%s normalized_examples=%s "
        "output=%s resume=%s overwrite=%s",
        artifacts.mutants,
        artifacts.normalized_examples,
        artifacts.equivalence,
        config.resume,
        config.overwrite,
    )
    if artifacts.equivalence.exists():
        if config.resume:
            existing_records = read_equivalence_records(artifacts.equivalence)
            resumed = True
        if not config.overwrite:
            if not config.resume:
                raise ValueError(
                    "Equivalence outputs already exist: equivalence.jsonl. "
                    "Set resume: true to reuse them or overwrite: true to replace them."
                )
        if config.overwrite:
            artifacts.equivalence.unlink()
            existing_records = []
            resumed = False
    else:
        existing_records = []
        resumed = False

    if config.tools.verieql_path is None:
        raise ValueError("tools.verieql_path is required when equivalence is enabled.")
    if config.tools.verieql_python is None:
        raise ValueError(
            "tools.verieql_python is required when equivalence is enabled."
        )
    for artifact_path, missing_message in (
        (
            artifacts.normalized_examples,
            "Equivalence filtering requires normalized_examples.jsonl, "
            "but it does not exist.",
        ),
        (
            artifacts.mutants,
            "Equivalence filtering requires mutants.jsonl, but it does not exist.",
        ),
    ):
        early = _missing_dependency_outcome(
            artifact_path=artifact_path,
            resumed=resumed,
            existing_records=existing_records,
            output_path=artifacts.equivalence,
            config=config,
            missing_message=missing_message,
        )
        if early is not None:
            return early

    examples = read_normalized_examples(artifacts.normalized_examples)
    mutations = read_mutation_records(artifacts.mutants)
    generated_mutations = [
        mutation
        for mutation in mutations
        if mutation.status == "generated" and mutation.mutant_sql is not None
    ]
    existing_ids = {record.mutant_id for record in existing_records}
    pending_mutations = [
        mutation
        for mutation in generated_mutations
        if mutation.mutant_id not in existing_ids
    ]
    LOGGER.info(
        "Equivalence selection generated=%s existing=%s pending=%s",
        len(generated_mutations),
        len(existing_records),
        len(pending_mutations),
    )
    if not pending_mutations:
        LOGGER.info("Equivalence stage reused existing artifact without pending work.")
        return summarize_equivalence(
            existing_records,
            output_path=artifacts.equivalence,
            resumed=resumed,
            skipped_stages=_skipped_stages(config),
        )

    input_rows = build_verieql_input_rows(pending_mutations, examples)
    _ensure_equivalence_artifact(artifacts.equivalence)

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
        LOGGER.info(
            "Launching VeriEQL cwd=%s timeout_per_pair=%s workers=%s "
            "batch_pairs=%s temp_dir=%s",
            config.tools.verieql_path,
            config.timeouts.verieql_seconds,
            config.workers,
            len(pending_mutations),
            temp_dir,
        )
        batch_timeout = _subprocess_timeout(
            config.timeouts.verieql_seconds,
            len(pending_mutations),
        )
        if runner is not None:
            records = _run_batch_with_runner(
                runner=runner,
                command=command,
                cwd=config.tools.verieql_path,
                batch_timeout=batch_timeout,
                output_path=output_path,
                mutations=pending_mutations,
                timeout_seconds=config.timeouts.verieql_seconds,
                artifact_path=artifacts.equivalence,
            )
        else:
            records = _run_batch_with_checkpointing(
                command=command,
                cwd=config.tools.verieql_path,
                batch_timeout=batch_timeout,
                output_path=output_path,
                mutations=pending_mutations,
                timeout_seconds=config.timeouts.verieql_seconds,
                artifact_path=artifacts.equivalence,
            )

    summary = summarize_equivalence(
        existing_records + records,
        output_path=artifacts.equivalence,
        resumed=resumed,
        skipped_stages=_skipped_stages(config),
    )
    LOGGER.info(
        "Equivalence stage complete checked=%s proven_equivalent=%s "
        "proven_non_equivalent=%s timeout=%s unsupported=%s error=%s output=%s",
        summary.checked_count,
        summary.proven_equivalent_count,
        summary.proven_non_equivalent_count,
        summary.timeout_count,
        summary.unsupported_count,
        summary.error_count,
        summary.output_path,
    )
    return summary


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
            _write_equivalence_jsonl_line(file, record=record)


def append_equivalence_record(path: Path, record: EquivalenceRecord) -> None:
    """Append one equivalence record to a JSONL artifact."""

    with path.open("a", encoding="utf-8") as file:
        _write_equivalence_jsonl_line(file, record=record)


def _write_equivalence_jsonl_line(file: TextIO, *, record: EquivalenceRecord) -> None:
    """Serialize one equivalence record as a single JSONL line."""

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


def _run_batch_with_runner(
    *,
    runner: VeriEQLRunner,
    command: list[str],
    cwd: Path,
    batch_timeout: int,
    output_path: Path,
    mutations: list[MutationRecord],
    timeout_seconds: int,
    artifact_path: Path,
) -> list[EquivalenceRecord]:
    """Run a synchronous test runner and salvage any written output rows."""

    failure: tuple[EquivalenceOutcome, str | None, str] | None = None
    try:
        runner(command, cwd=cwd, timeout=batch_timeout)
    except subprocess.TimeoutExpired as exc:
        LOGGER.warning(
            "VeriEQL runner timed out timeout=%s error=%s",
            batch_timeout,
            exc,
        )
        failure = ("timeout", "TMO", str(exc))
    except subprocess.CalledProcessError as exc:
        LOGGER.warning(
            "VeriEQL runner failed code=%s stderr=%s",
            exc.returncode,
            exc.stderr or exc.stdout or str(exc),
        )
        failure = ("error", None, exc.stderr or exc.stdout or str(exc))
    return _checkpoint_and_finalize_records(
        output_path=output_path,
        mutations=mutations,
        timeout_seconds=timeout_seconds,
        artifact_path=artifact_path,
        failure=failure,
    )


def _run_batch_with_checkpointing(
    *,
    command: list[str],
    cwd: Path,
    batch_timeout: int,
    output_path: Path,
    mutations: list[MutationRecord],
    timeout_seconds: int,
    artifact_path: Path,
) -> list[EquivalenceRecord]:
    """Run VeriEQL and checkpoint shard outputs while it is still executing."""

    stdout_path = output_path.parent / "stdout.log"
    stderr_path = output_path.parent / "stderr.log"
    start = time.monotonic()
    poll_seconds = 1.0
    records: list[EquivalenceRecord] = []
    seen_ids = {
        record.mutant_id for record in read_equivalence_records(artifact_path)
    }
    with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        timed_out = False
        while True:
            _checkpoint_available_rows(
                output_path=output_path,
                mutations=mutations,
                timeout_seconds=timeout_seconds,
                artifact_path=artifact_path,
                records=records,
                seen_ids=seen_ids,
            )
            try:
                return_code = process.wait(timeout=poll_seconds)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - start >= batch_timeout:
                    process.kill()
                    process.wait()
                    timed_out = True
                    return_code = None
                    LOGGER.warning(
                        "VeriEQL batch timeout reached batch_timeout=%s output_dir=%s",
                        batch_timeout,
                        output_path.parent,
                    )
                    break
        stdout_text = stdout_path.read_text(encoding="utf-8")
        stderr_text = stderr_path.read_text(encoding="utf-8")
    if timed_out:
        failure = (
            "timeout",
            "TMO",
            f"VeriEQL batch timed out after {batch_timeout} seconds.",
        )
    elif return_code != 0:
        failure = (
            "error",
            None,
            stderr_text or stdout_text or f"VeriEQL exited with code {return_code}.",
        )
    else:
        failure = None
    return _checkpoint_and_finalize_records(
        output_path=output_path,
        mutations=mutations,
        timeout_seconds=timeout_seconds,
        artifact_path=artifact_path,
        failure=failure,
        records=records,
        seen_ids=seen_ids,
    )


def _checkpoint_and_finalize_records(
    *,
    output_path: Path,
    mutations: list[MutationRecord],
    timeout_seconds: int,
    artifact_path: Path,
    failure: tuple[EquivalenceOutcome, str | None, str] | None,
    records: list[EquivalenceRecord] | None = None,
    seen_ids: set[str] | None = None,
) -> list[EquivalenceRecord]:
    """Append all discovered rows, then synthesize explicit outcomes for the rest."""

    records = records or []
    seen_ids = seen_ids or {
        record.mutant_id for record in read_equivalence_records(artifact_path)
    }
    _checkpoint_available_rows(
        output_path=output_path,
        mutations=mutations,
        timeout_seconds=timeout_seconds,
        artifact_path=artifact_path,
        records=records,
        seen_ids=seen_ids,
    )
    missing_filled = 0
    for mutation in mutations:
        if mutation.mutant_id in seen_ids:
            continue
        if failure is None:
            record = _error_record(
                mutation,
                timeout_seconds=timeout_seconds,
                error="VeriEQL did not write a result row for this mutant.",
            )
        else:
            outcome, raw_state, error = failure
            record = EquivalenceRecord(
                mutant_id=mutation.mutant_id,
                example_id=mutation.example_id,
                outcome=outcome,
                raw_state=raw_state,
                timeout_seconds=timeout_seconds,
                error=error,
            )
        _record_result(records, record, output_path=artifact_path)
        seen_ids.add(record.mutant_id)
        missing_filled += 1
    LOGGER.info(
        "Equivalence checkpoint finalized records=%s missing_filled=%s artifact=%s",
        len(records),
        missing_filled,
        artifact_path,
    )
    return records


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


def _checkpoint_available_rows(
    *,
    output_path: Path,
    mutations: list[MutationRecord],
    timeout_seconds: int,
    artifact_path: Path,
    records: list[EquivalenceRecord],
    seen_ids: set[str],
) -> None:
    """Read available VeriEQL rows and append any not yet checkpointed."""

    appended = 0
    rows_by_file = _read_raw_verieql_rows(output_path)
    mutations_by_id = {mutation.mutant_id: mutation for mutation in mutations}
    for mutant_id, row in rows_by_file.items():
        if mutant_id in seen_ids:
            continue
        mutation = mutations_by_id.get(mutant_id)
        if mutation is None:
            continue
        states = row.get("states", [])
        raw_state = _last_state(states)
        record = EquivalenceRecord(
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
        _record_result(records, record, output_path=artifact_path)
        seen_ids.add(mutant_id)
        appended += 1
    if appended:
        LOGGER.info(
            "Equivalence checkpoint appended=%s artifact=%s source_dir=%s",
            appended,
            artifact_path,
            output_path.parent,
        )


def _read_raw_verieql_rows(path: Path) -> dict[str, dict[str, Any]]:
    """Read raw VeriEQL output keyed by the input ``file`` field."""

    rows: dict[str, dict[str, Any]] = {}
    output_paths = sorted(
        {candidate for candidate in path.parent.glob(f"{path.name}*")}
    )
    if path.exists():
        output_paths.append(path)
    for candidate in output_paths:
        if not candidate.exists() or not candidate.is_file():
            continue
        with candidate.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    rows[f"__malformed_{candidate.name}_{line_number}"] = {
                        "file": f"__malformed_{candidate.name}_{line_number}",
                        "states": ["OTE"],
                        "err": (
                            f"Malformed VeriEQL output in {candidate.name} at line "
                            f"{line_number}."
                        ),
                    }
                    continue
                if isinstance(row, dict) and isinstance(row.get("file"), str):
                    rows[str(row["file"])] = row
    return rows


def _subprocess_failure_records(
    mutations: list[MutationRecord],
    *,
    outcome: EquivalenceOutcome,
    raw_state: str | None,
    timeout_seconds: int,
    error: str,
) -> list[EquivalenceRecord]:
    """Build one record per generated mutant after batch subprocess failure."""

    return [
        EquivalenceRecord(
            mutant_id=mutation.mutant_id,
            example_id=mutation.example_id,
            outcome=outcome,
            raw_state=raw_state,
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


def _ensure_equivalence_artifact(path: Path) -> None:
    """Create an empty checkpoint artifact when it does not exist yet."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")


def _record_result(
    records: list[EquivalenceRecord],
    record: EquivalenceRecord,
    *,
    output_path: Path | None,
) -> None:
    """Keep the in-memory summary and on-disk checkpoint in sync."""

    records.append(record)
    if output_path is not None:
        append_equivalence_record(output_path, record)


def _missing_dependency_outcome(
    *,
    artifact_path: Path,
    resumed: bool,
    existing_records: list[EquivalenceRecord],
    output_path: Path,
    config: ExperimentConfig,
    missing_message: str,
) -> EquivalenceSummary | None:
    """If a required artifact is absent, return a resume summary or raise."""

    if artifact_path.exists():
        return None
    if resumed:
        return summarize_equivalence(
            existing_records,
            output_path=output_path,
            resumed=True,
            skipped_stages=_skipped_stages(config),
        )
    raise ValueError(missing_message)


def _skipped_stages(config: ExperimentConfig) -> list[str]:
    """Return enabled stages after equivalence that remain unimplemented."""

    return [
        stage
        for stage in config.stages.enabled()
        if stage not in {"normalize", "mutate", "equivalence"}
    ]
