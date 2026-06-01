"""Spider 2.0 SQLite-subset benchmark normalization."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from donatello.benchmarks.base import BenchmarkAdapter, BenchmarkError
from donatello.config import ExperimentConfig
from donatello.models import NormalizedExample, NormalizedSchema

EXPECTED_INSTANCE_IDS = [
    "local003",
    "local004",
    "local008",
    "local017",
    "local019",
    "local022",
    "local023",
    "local029",
    "local038",
    "local039",
    "local058",
    "local065",
    "local066",
    "local075",
    "local078",
    "local099",
    "local131",
    "local163",
    "local197",
    "local199",
    "local210",
    "local219",
    "local301",
    "local309",
]


class Spider2Adapter(BenchmarkAdapter):
    """Normalize the local Spider 2.0 SQLite-backed subset into the shared contract."""

    def normalize(self, config: ExperimentConfig) -> list[NormalizedExample]:
        if config.benchmark != "spider2":
            raise BenchmarkError("Spider 2.0 normalizer received a non-Spider config.")

        data_root = config.data_root.resolve()
        metadata_path = data_root / "spider2-lite.jsonl"
        gold_sql_dir = data_root / "gold_sql"
        local_sqlite_dir = data_root / "local_sqlite"
        local_map_path = local_sqlite_dir / "local-map.jsonl"

        metadata_by_id = _load_metadata(metadata_path)
        local_map = _load_local_map(local_map_path)
        gold_sql_by_id = _load_gold_sql(gold_sql_dir)

        available_ids = set(metadata_by_id) & set(local_map) & set(gold_sql_by_id)
        missing_expected = [
            instance_id
            for instance_id in EXPECTED_INSTANCE_IDS
            if instance_id not in available_ids
        ]
        unexpected_available = sorted(available_ids - set(EXPECTED_INSTANCE_IDS))
        if missing_expected or unexpected_available:
            details: list[str] = []
            if missing_expected:
                details.append(f"missing expected ids: {', '.join(missing_expected)}")
            if unexpected_available:
                details.append(
                    f"unexpected available ids: {', '.join(unexpected_available)}"
                )
            raise BenchmarkError(
                "Spider 2.0 SQLite subset did not resolve to the expected "
                "24-question set: "
                + "; ".join(details)
            )

        schemas: dict[str, NormalizedSchema] = {}
        examples: list[NormalizedExample] = []
        for instance_id in EXPECTED_INSTANCE_IDS:
            db_id = local_map[instance_id]
            sqlite_path = (local_sqlite_dir / f"{db_id}.sqlite").resolve()
            if not sqlite_path.exists():
                raise BenchmarkError(
                    "Missing Spider 2.0 SQLite database for db_id "
                    f"'{db_id}': {sqlite_path}"
                )
            schema = schemas.setdefault(db_id, _introspect_schema(sqlite_path))
            metadata = metadata_by_id[instance_id]
            question = _required_metadata_string(metadata, "question", instance_id)
            gold_sql = gold_sql_by_id[instance_id]
            examples.append(
                NormalizedExample(
                    example_id=f"spider2:dev:{instance_id}",
                    benchmark="spider2",
                    subset="dev",
                    source_id=instance_id,
                    db_id=db_id,
                    question=question,
                    gold_sql=gold_sql,
                    sqlite_path=str(sqlite_path),
                    sqlite_relpath=str(sqlite_path.relative_to(data_root)),
                    schema=schema,
                    metadata={
                        "instance_id": instance_id,
                        "db": db_id,
                        "external_knowledge": metadata.get("external_knowledge"),
                        "original_question": question,
                    },
                )
            )
        return examples


def _load_metadata(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise BenchmarkError(
            f"Required Spider 2.0 metadata file does not exist: {path}"
        )
    results: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(
                f"Invalid JSON on line {line_number} in {path}: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise BenchmarkError(f"Line {line_number} in {path} must be a JSON object.")
        instance_id = row.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise BenchmarkError(
                f"Line {line_number} in {path} is missing instance_id."
            )
        results[instance_id] = row
    return results


def _load_local_map(path: Path) -> dict[str, str]:
    if not path.exists():
        raise BenchmarkError(f"Required Spider 2.0 local map does not exist: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BenchmarkError(f"Spider 2.0 local map must be a JSON object: {path}")
    result: dict[str, str] = {}
    for instance_id, db_id in raw.items():
        if not isinstance(instance_id, str) or not isinstance(db_id, str):
            raise BenchmarkError(
                f"Spider 2.0 local map must use string keys and values: {path}"
            )
        result[instance_id] = db_id
    return result


def _load_gold_sql(path: Path) -> dict[str, str]:
    if not path.exists():
        raise BenchmarkError(
            f"Required Spider 2.0 gold SQL directory does not exist: {path}"
        )
    results: dict[str, str] = {}
    for sql_path in sorted(path.glob("*.sql")):
        sql = sql_path.read_text(encoding="utf-8").strip()
        if sql:
            results[sql_path.stem] = sql
    return results


def _required_metadata_string(
    metadata: dict[str, Any],
    field: str,
    instance_id: str,
) -> str:
    value = metadata.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError(
            f"Spider 2.0 metadata for {instance_id} is missing string field '{field}'."
        )
    return value


def _introspect_schema(sqlite_path: Path) -> NormalizedSchema:
    connection = sqlite3.connect(f"file:{sqlite_path}?mode=ro&immutable=1", uri=True)
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        ]
        table_names = list(tables)
        table_names_original = list(tables)
        column_names: list[tuple[int, str]] = [(-1, "*")]
        column_names_original: list[tuple[int, str]] = [(-1, "*")]
        column_types: list[str] = ["text"]
        primary_keys: list[int | tuple[int, ...]] = []
        foreign_keys: list[tuple[int, int]] = []
        column_index_by_table_and_name: dict[tuple[str, str], int] = {}

        for table_index, table_name in enumerate(tables):
            table_info = connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
            primary_key_parts: list[tuple[int, int]] = []
            for row in table_info:
                column_name = str(row[1])
                raw_type = row[2]
                column_type = str(raw_type).strip().lower() if raw_type else "text"
                global_index = len(column_names)
                column_names.append((table_index, column_name))
                column_names_original.append((table_index, column_name))
                column_types.append(column_type)
                column_index_by_table_and_name[(table_name, column_name)] = global_index
                pk_position = int(row[5])
                if pk_position > 0:
                    primary_key_parts.append((pk_position, global_index))
            if len(primary_key_parts) == 1:
                primary_keys.append(primary_key_parts[0][1])
            elif primary_key_parts:
                primary_keys.append(
                    tuple(index for _, index in sorted(primary_key_parts))
                )

        for table_name in tables:
            fk_rows = connection.execute(
                f'PRAGMA foreign_key_list("{table_name}")'
            ).fetchall()
            for row in fk_rows:
                target_table = str(row[2])
                source_column = str(row[3])
                target_column = str(row[4])
                source_index = column_index_by_table_and_name.get(
                    (table_name, source_column)
                )
                target_index = column_index_by_table_and_name.get(
                    (target_table, target_column)
                )
                if source_index is None or target_index is None:
                    continue
                foreign_keys.append((source_index, target_index))

        return NormalizedSchema(
            table_names=table_names,
            table_names_original=table_names_original,
            column_names=column_names,
            column_names_original=column_names_original,
            column_types=column_types,
            primary_keys=primary_keys,
            foreign_keys=foreign_keys,
        )
    finally:
        connection.close()
