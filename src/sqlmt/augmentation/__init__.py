"""Database augmentation from SQLDriller counterexamples."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from sqlmt.artifacts import ExperimentArtifacts
from sqlmt.config import ExperimentConfig
from sqlmt.counterexamples import read_counterexample_records
from sqlmt.equivalence import read_equivalence_records
from sqlmt.execution import (
    execute_mutations,
    read_execution_records,
    summarize_execution,
)
from sqlmt.models import (
    AugmentationRecord,
    AugmentationSummary,
    CounterexampleRecord,
    MutationRecord,
)
from sqlmt.mutation.generator import read_mutation_records

LOGGER = logging.getLogger(__name__)


def run_augmentation(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
) -> AugmentationSummary:
    """Build augmented database copies and rerun covered surviving mutants."""

    LOGGER.info(
        "Augmentation stage start counterexamples=%s output=%s resume=%s overwrite=%s",
        artifacts.counterexamples,
        artifacts.augmentation,
        config.resume,
        config.overwrite,
    )
    existing_outputs = [
        path
        for path in (artifacts.augmentation, artifacts.augmented_execution)
        if path.exists()
    ]
    if existing_outputs:
        if config.resume:
            LOGGER.info(
                "Augmentation stage resume outputs=%s",
                ",".join(path.name for path in existing_outputs),
            )
            return _summarize_existing_augmentation(config, artifacts)
        if not config.overwrite:
            raise ValueError(
                "Augmentation outputs already exist: "
                + ", ".join(path.name for path in existing_outputs)
                + ". Set resume: true to reuse them or overwrite: true to replace "
                "them."
            )

    if config.overwrite:
        if artifacts.augmented_databases_dir.exists():
            LOGGER.info(
                "Augmentation overwrite removing directory=%s",
                artifacts.augmented_databases_dir,
            )
            shutil.rmtree(artifacts.augmented_databases_dir)
        if artifacts.augmented_execution.exists():
            LOGGER.info(
                "Augmentation overwrite removing output=%s",
                artifacts.augmented_execution,
            )
            artifacts.augmented_execution.unlink()

    for artifact_path, missing_message in (
        (
            artifacts.mutants,
            "Augmentation requires mutants.jsonl, but it does not exist.",
        ),
        (
            artifacts.equivalence,
            "Augmentation requires equivalence.jsonl, but it does not exist.",
        ),
        (
            artifacts.execution,
            "Augmentation requires execution.jsonl, but it does not exist.",
        ),
        (
            artifacts.counterexamples,
            "Augmentation requires counterexamples.jsonl, but it does not exist.",
        ),
    ):
        if not artifact_path.exists():
            raise ValueError(missing_message)

    mutations = read_mutation_records(artifacts.mutants)
    equivalence_records = read_equivalence_records(artifacts.equivalence)
    execution_records = read_execution_records(artifacts.execution)
    counterexample_records = read_counterexample_records(artifacts.counterexamples)

    selected = _selected_counterexamples(counterexample_records)
    artifacts.augmented_databases_dir.mkdir(parents=True, exist_ok=True)
    mutation_by_id = {mutation.mutant_id: mutation for mutation in mutations}
    groups = _group_by_source(selected)
    LOGGER.info(
        "Augmentation selection counterexample_records=%s selected=%s "
        "source_databases=%s mutations=%s",
        len(counterexample_records),
        len(selected),
        len(groups),
        len(mutations),
    )
    augmentation_records = [
        _augment_database(source_path, records, artifacts=artifacts)
        for source_path, records in groups.items()
    ]
    write_augmentation_records(artifacts.augmentation, augmentation_records)

    rerun_mutations = _rerun_mutations(
        augmentation_records,
        mutation_by_id=mutation_by_id,
        proven_non_equivalent_ids={
            record.mutant_id
            for record in equivalence_records
            if record.outcome == "proven_non_equivalent"
        },
        survived_ids={
            record.mutant_id
            for record in execution_records
            if record.outcome == "survived"
        },
    )
    LOGGER.info(
        "Augmentation rerun selected_mutants=%s output=%s",
        len(rerun_mutations),
        artifacts.augmented_execution,
    )
    if artifacts.augmented_execution.exists():
        artifacts.augmented_execution.unlink()
    artifacts.augmented_execution.touch()
    augmented_execution_records = execute_mutations(
        rerun_mutations,
        timeout_seconds=config.timeouts.sqlite_seconds,
        output_path=artifacts.augmented_execution,
    )
    execution_summary = summarize_execution(
        augmented_execution_records,
        output_path=artifacts.augmented_execution,
        skipped_stages=_skipped_stages(config),
    )
    summary = summarize_augmentation(
        augmentation_records,
        execution_summary=execution_summary,
        output_path=artifacts.augmentation,
        output_dir=artifacts.augmented_databases_dir,
        skipped_stages=_skipped_stages(config),
    )
    LOGGER.info(
        "Augmentation stage complete databases=%s counterexamples=%s "
        "inserted_rows=%s skipped_rows=%s augmented_score=%s output=%s",
        summary.database_count,
        summary.counterexample_count,
        summary.inserted_row_count,
        summary.skipped_row_count,
        summary.augmented_execution.mutation_score,
        summary.output_path,
    )
    return summary


def read_augmentation_records(path: Path) -> list[AugmentationRecord]:
    """Read augmentation records from a JSONL artifact."""

    records: list[AugmentationRecord] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"augmentation.jsonl is not valid JSONL: line {line_number}."
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(
                    f"augmentation.jsonl row {line_number} must be a JSON object."
                )
            records.append(AugmentationRecord.from_dict(row))
    return records


def write_augmentation_records(path: Path, records: list[AugmentationRecord]) -> None:
    """Write augmentation records as JSONL."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            _write_augmentation_jsonl_line(file, record=record)


def summarize_augmentation(
    records: list[AugmentationRecord],
    *,
    execution_summary,
    output_path: Path,
    output_dir: Path,
    resumed: bool = False,
    skipped_stages: list[str] | None = None,
) -> AugmentationSummary:
    """Aggregate augmentation counts."""

    return AugmentationSummary(
        database_count=len(records),
        counterexample_count=sum(record.counterexample_count for record in records),
        inserted_row_count=sum(record.inserted_row_count for record in records),
        skipped_row_count=sum(record.skipped_row_count for record in records),
        warning_count=sum(len(record.warnings) for record in records),
        error_count=sum(len(record.errors) for record in records),
        output_path=str(output_path),
        output_dir=str(output_dir),
        augmented_execution=execution_summary,
        resumed=resumed,
        skipped_stages=list(skipped_stages or []),
    )


def _selected_counterexamples(
    records: list[CounterexampleRecord],
) -> list[CounterexampleRecord]:
    return [
        record
        for record in records
        if record.outcome == "generated"
        and record.counterexample_path is not None
        and Path(record.counterexample_path).exists()
    ]


def _group_by_source(
    records: list[CounterexampleRecord],
) -> dict[Path, list[CounterexampleRecord]]:
    grouped: dict[Path, list[CounterexampleRecord]] = defaultdict(list)
    for record in records:
        grouped[Path(record.source_sqlite_path)].append(record)
    return dict(grouped)


def _augment_database(
    source_path: Path,
    records: list[CounterexampleRecord],
    *,
    artifacts: ExperimentArtifacts,
) -> AugmentationRecord:
    db_id = _db_id(records, source_path)
    target_path = artifacts.augmented_databases_dir / _target_name(source_path, db_id)
    warnings: list[str] = []
    errors: list[str] = []
    inserted = 0
    skipped = 0

    try:
        shutil.copy2(source_path, target_path)
    except OSError as exc:
        LOGGER.warning(
            "Augmentation database copy failed source=%s target=%s error=%s",
            source_path,
            target_path,
            exc,
        )
        return AugmentationRecord(
            source_sqlite_path=str(source_path),
            augmented_sqlite_path=str(target_path),
            db_id=db_id,
            mutant_ids=_mutant_ids(records),
            counterexample_count=len(records),
            inserted_row_count=0,
            skipped_row_count=0,
            errors=[f"database copy failed: {exc}"],
        )
    LOGGER.info(
        "Augmentation database copied source=%s target=%s counterexamples=%s",
        source_path,
        target_path,
        len(records),
    )

    for record in records:
        counterexample_path = Path(record.counterexample_path or "")
        try:
            result = _merge_counterexample(counterexample_path, target_path)
        except sqlite3.Error as exc:
            errors.append(f"{record.mutant_id}: counterexample merge failed: {exc}")
            LOGGER.warning(
                "Augmentation counterexample merge failed mutant_id=%s "
                "counterexample=%s target=%s error=%s",
                record.mutant_id,
                counterexample_path,
                target_path,
                exc,
            )
            continue
        inserted += result.inserted
        skipped += result.skipped
        for warning in result.warnings:
            warnings.append(f"{record.mutant_id}: {warning}")
            LOGGER.warning(
                "Augmentation merge warning mutant_id=%s counterexample=%s "
                "target=%s warning=%s",
                record.mutant_id,
                counterexample_path,
                target_path,
                warning,
            )
        LOGGER.info(
            "Augmentation counterexample merged mutant_id=%s counterexample=%s "
            "target=%s inserted_rows=%s skipped_rows=%s warnings=%s",
            record.mutant_id,
            counterexample_path,
            target_path,
            result.inserted,
            result.skipped,
            len(result.warnings),
        )

    return AugmentationRecord(
        source_sqlite_path=str(source_path),
        augmented_sqlite_path=str(target_path),
        db_id=db_id,
        mutant_ids=_mutant_ids(records),
        counterexample_count=len(records),
        inserted_row_count=inserted,
        skipped_row_count=skipped,
        warnings=warnings,
        errors=errors,
    )


class _MergeResult:
    def __init__(self) -> None:
        self.inserted = 0
        self.skipped = 0
        self.warnings: list[str] = []


def _merge_counterexample(source_path: Path, target_path: Path) -> _MergeResult:
    result = _MergeResult()
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    try:
        source.row_factory = sqlite3.Row
        for table in _table_names(source):
            source_columns = _column_names(source, table)
            row_count = _row_count(source, table)
            if table not in _table_names(target):
                result.skipped += row_count
                result.warnings.append(f"missing table skipped: {table}")
                continue
            target_columns = _column_names(target, table)
            unknown_columns = [
                column for column in source_columns if column not in target_columns
            ]
            if unknown_columns:
                result.skipped += row_count
                result.warnings.append(
                    "column mismatch skipped: "
                    f"{table} missing {', '.join(unknown_columns)}"
                )
                continue
            for row in source.execute(
                f"SELECT {_quoted_csv(source_columns)} FROM {_quote_identifier(table)}"
            ):
                values = [row[column] for column in source_columns]
                if _row_exists(target, table, source_columns, values):
                    result.skipped += 1
                    result.warnings.append(f"duplicate row skipped: {table}")
                    continue
                placeholders = ", ".join("?" for _ in source_columns)
                cursor = target.execute(
                    "INSERT OR IGNORE INTO "
                    f"{_quote_identifier(table)} ({_quoted_csv(source_columns)}) "
                    f"VALUES ({placeholders})",
                    values,
                )
                if cursor.rowcount == 1:
                    result.inserted += 1
                else:
                    result.skipped += 1
                    result.warnings.append(f"duplicate row skipped: {table}")
        target.commit()
    finally:
        source.close()
        target.close()
    return result


def _rerun_mutations(
    records: list[AugmentationRecord],
    *,
    mutation_by_id: dict[str, MutationRecord],
    proven_non_equivalent_ids: set[str],
    survived_ids: set[str],
) -> list[MutationRecord]:
    rerun: list[MutationRecord] = []
    for record in records:
        if record.errors:
            continue
        for mutant_id in record.mutant_ids:
            if (
                mutant_id not in proven_non_equivalent_ids
                or mutant_id not in survived_ids
                or mutant_id not in mutation_by_id
            ):
                continue
            rerun.append(
                replace(
                    mutation_by_id[mutant_id],
                    sqlite_path=record.augmented_sqlite_path,
                )
            )
    return rerun


def _summarize_existing_augmentation(
    config: ExperimentConfig,
    artifacts: ExperimentArtifacts,
) -> AugmentationSummary:
    if not artifacts.augmentation.exists():
        raise ValueError("resume is true but augmentation.jsonl does not exist.")
    records = read_augmentation_records(artifacts.augmentation)
    execution_records = (
        read_execution_records(artifacts.augmented_execution)
        if artifacts.augmented_execution.exists()
        else []
    )
    execution_summary = summarize_execution(
        execution_records,
        output_path=artifacts.augmented_execution,
        resumed=True,
        skipped_stages=_skipped_stages(config),
    )
    LOGGER.info(
        "Augmentation resume summary databases=%s execution_records=%s output=%s",
        len(records),
        len(execution_records),
        artifacts.augmentation,
    )
    return summarize_augmentation(
        records,
        execution_summary=execution_summary,
        output_path=artifacts.augmentation,
        output_dir=artifacts.augmented_databases_dir,
        resumed=True,
        skipped_stages=_skipped_stages(config),
    )


def _write_augmentation_jsonl_line(
    file: TextIO,
    *,
    record: AugmentationRecord,
) -> None:
    file.write(json.dumps(record.to_dict(), ensure_ascii=True))
    file.write("\n")


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _column_names(connection: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    ]


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
        ).fetchone()[0]
    )


def _row_exists(
    connection: sqlite3.Connection,
    table: str,
    columns: list[str],
    values: list[object],
) -> bool:
    predicates = " AND ".join(f"{_quote_identifier(column)} IS ?" for column in columns)
    return (
        connection.execute(
            f"SELECT 1 FROM {_quote_identifier(table)} WHERE {predicates} LIMIT 1",
            values,
        ).fetchone()
        is not None
    )


def _quoted_csv(names: list[str]) -> str:
    return ", ".join(_quote_identifier(name) for name in names)


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _target_name(source_path: Path, db_id: str) -> str:
    digest = hashlib.sha1(str(source_path.resolve()).encode("utf-8")).hexdigest()[:10]
    safe_db_id = "".join(
        character if character.isalnum() else "_" for character in db_id
    )
    return f"{safe_db_id}-{digest}.sqlite"


def _db_id(records: list[CounterexampleRecord], source_path: Path) -> str:
    for record in records:
        db_id = record.metadata.get("db_id")
        if isinstance(db_id, str) and db_id:
            return db_id
    return source_path.stem


def _mutant_ids(records: list[CounterexampleRecord]) -> list[str]:
    return sorted({record.mutant_id for record in records})


def _skipped_stages(config: ExperimentConfig) -> list[str]:
    return [
        stage
        for stage in config.stages.enabled()
        if stage
        not in {
            "normalize",
            "mutate",
            "equivalence",
            "execute",
            "counterexamples",
            "augment",
        }
    ]
