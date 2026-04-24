"""BIRD benchmark normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlmt.benchmarks.base import BenchmarkAdapter, BenchmarkError
from sqlmt.config import ExperimentConfig
from sqlmt.models import NormalizedExample, NormalizedSchema

SPLIT_FILES: dict[str, dict[str, str]] = {
    "dev": {
        "examples": "dev.json",
        "tables": "dev_tables.json",
        "databases": "dev_databases",
    },
    "train": {
        "examples": "train.json",
        "tables": "train_tables.json",
        "databases": "train_databases",
    },
}


class BirdAdapter(BenchmarkAdapter):
    """Normalize BIRD examples into the shared example contract."""

    def normalize(self, config: ExperimentConfig) -> list[NormalizedExample]:
        if config.benchmark != "bird":
            raise BenchmarkError("BIRD normalizer received a non-BIRD config.")

        data_root = config.data_root.resolve()
        examples: list[NormalizedExample] = []

        for split in _splits_for_mode(config.bird_mode):
            split_dir = data_root / split
            layout = SPLIT_FILES[split]
            examples_path = split_dir / layout["examples"]
            raw_examples = _load_json_array(examples_path)
            schema_by_db = _load_schema_map(split_dir / layout["tables"])
            db_root = split_dir / layout["databases"]

            for index, raw_example in enumerate(raw_examples):
                examples.append(
                    _normalize_example(
                        data_root=data_root,
                        split=split,
                        index=index,
                        raw_example=raw_example,
                        schema_by_db=schema_by_db,
                        db_root=db_root,
                        examples_path=examples_path,
                    )
                )

        return examples


def _splits_for_mode(mode: str | None) -> list[str]:
    if mode is None:
        raise BenchmarkError("bird_mode is required for benchmark 'bird'.")
    if mode == "full":
        return ["dev", "train"]
    if mode in SPLIT_FILES:
        return [mode]
    raise BenchmarkError(f"Unsupported BIRD mode: {mode}")


def _load_json_array(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise BenchmarkError(f"Required BIRD file does not exist: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(raw, list):
        raise BenchmarkError(f"BIRD file must contain a JSON array: {path}")

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BenchmarkError(f"Row {index} in {path} must be a JSON object.")
        typed_item: dict[str, Any] = {}
        for key, value in item.items():
            if not isinstance(key, str):
                raise BenchmarkError(f"Row {index} in {path} must use string keys.")
            typed_item[key] = value
        rows.append(typed_item)
    return rows


def _load_schema_map(path: Path) -> dict[str, NormalizedSchema]:
    schemas: dict[str, NormalizedSchema] = {}

    for index, item in enumerate(_load_json_array(path)):
        db_id = _required_string(item, "db_id", path, index)
        schemas[db_id] = NormalizedSchema(
            table_names=_string_list(item, "table_names", path, index),
            table_names_original=_string_list(
                item, "table_names_original", path, index
            ),
            column_names=_int_string_pairs(item, "column_names", path, index),
            column_names_original=_int_string_pairs(
                item, "column_names_original", path, index
            ),
            column_types=_string_list(item, "column_types", path, index),
            primary_keys=_primary_keys(item, "primary_keys", path, index),
            foreign_keys=_int_pairs(item, "foreign_keys", path, index),
        )

    return schemas


def _normalize_example(
    *,
    data_root: Path,
    split: str,
    index: int,
    raw_example: dict[str, Any],
    schema_by_db: dict[str, NormalizedSchema],
    db_root: Path,
    examples_path: Path,
) -> NormalizedExample:
    db_id = _required_string(raw_example, "db_id", examples_path, index)
    question = _required_string(raw_example, "question", examples_path, index)
    gold_sql = _required_string(raw_example, "SQL", examples_path, index)

    if db_id not in schema_by_db:
        raise BenchmarkError(
            f"Example row {index} in split '{split}' references unknown db_id: {db_id}"
        )

    sqlite_path = db_root / db_id / f"{db_id}.sqlite"
    if not sqlite_path.exists():
        raise BenchmarkError(
            f"Missing SQLite database for db_id '{db_id}': {sqlite_path}"
        )

    source_id: str | None = None
    metadata: dict[str, Any] = {}

    if "question_id" in raw_example:
        question_id = raw_example["question_id"]
        if isinstance(question_id, bool) or not isinstance(question_id, int):
            raise BenchmarkError(
                f"Example row {index} in split '{split}' has invalid question_id."
            )
        source_id = str(question_id)
        metadata["question_id"] = question_id

    evidence = raw_example.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, str):
            raise BenchmarkError(
                f"Example row {index} in split '{split}' has non-string evidence."
            )
        metadata["evidence"] = evidence

    difficulty = raw_example.get("difficulty")
    if difficulty is not None:
        if not isinstance(difficulty, str):
            raise BenchmarkError(
                f"Example row {index} in split '{split}' has non-string difficulty."
            )
        metadata["difficulty"] = difficulty

    resolved_sqlite = sqlite_path.resolve()

    return NormalizedExample(
        example_id=f"bird:{split}:{index:06d}",
        benchmark="bird",
        subset=split,
        source_id=source_id,
        db_id=db_id,
        question=question,
        gold_sql=gold_sql,
        sqlite_path=str(resolved_sqlite),
        sqlite_relpath=str(resolved_sqlite.relative_to(data_root)),
        schema=schema_by_db[db_id],
        metadata=metadata,
    )


def _required_string(
    mapping: dict[str, Any], field: str, path: Path, index: int
) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError(
            f"Row {index} in {path} is missing string field '{field}'."
        )
    return value


def _string_list(
    mapping: dict[str, Any], field: str, path: Path, index: int
) -> list[str]:
    value = mapping.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BenchmarkError(f"Row {index} in {path} has invalid '{field}'.")
    return value


def _primary_keys(
    mapping: dict[str, Any], field: str, path: Path, index: int
) -> list[int | tuple[int, ...]]:
    value = mapping.get(field)
    if not isinstance(value, list):
        raise BenchmarkError(f"Row {index} in {path} has invalid '{field}'.")
    result: list[int | tuple[int, ...]] = []
    for item in value:
        if isinstance(item, bool):
            raise BenchmarkError(f"Row {index} in {path} has invalid '{field}'.")
        if isinstance(item, int):
            result.append(item)
            continue
        if isinstance(item, list):
            key_parts: list[int] = []
            for part in item:
                if isinstance(part, bool) or not isinstance(part, int):
                    raise BenchmarkError(
                        f"Row {index} in {path} has invalid '{field}'."
                    )
                key_parts.append(part)
            result.append(tuple(key_parts))
            continue
        raise BenchmarkError(f"Row {index} in {path} has invalid '{field}'.")
    return result


def _int_string_pairs(
    mapping: dict[str, Any], field: str, path: Path, index: int
) -> list[tuple[int, str]]:
    value = mapping.get(field)
    if not isinstance(value, list):
        raise BenchmarkError(f"Row {index} in {path} has invalid '{field}'.")
    result: list[tuple[int, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or isinstance(item[0], bool)
            or not isinstance(item[0], int)
            or not isinstance(item[1], str)
        ):
            raise BenchmarkError(f"Row {index} in {path} has invalid '{field}'.")
        result.append((item[0], item[1]))
    return result


def _int_pairs(
    mapping: dict[str, Any], field: str, path: Path, index: int
) -> list[tuple[int, int]]:
    value = mapping.get(field)
    if not isinstance(value, list):
        raise BenchmarkError(f"Row {index} in {path} has invalid '{field}'.")
    result: list[tuple[int, int]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or isinstance(item[0], bool)
            or not isinstance(item[0], int)
            or isinstance(item[1], bool)
            or not isinstance(item[1], int)
        ):
            raise BenchmarkError(f"Row {index} in {path} has invalid '{field}'.")
        result.append((item[0], item[1]))
    return result
