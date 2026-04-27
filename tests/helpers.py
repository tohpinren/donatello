"""Test fixtures shared across normalization and mutation suites."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlmt.models import NormalizedExample, NormalizedSchema


def write_bird_fixture(root: Path) -> Path:
    """Create a tiny extracted BIRD-style fixture tree under ``root``."""

    data_root = root / "bird"
    _write_split(
        data_root=data_root,
        split="dev",
        examples=[
            {
                "question_id": 7,
                "db_id": "dev_db",
                "question": "How many rows are in dev_table?",
                "SQL": "SELECT COUNT(*) FROM dev_table",
                "evidence": "Use COUNT(*)",
                "difficulty": "simple",
            }
        ],
        tables=[
            {
                "db_id": "dev_db",
                "table_names": ["dev_table"],
                "table_names_original": ["dev_table"],
                "column_names": [[-1, "*"], [0, "id"]],
                "column_names_original": [[-1, "*"], [0, "id"]],
                "column_types": ["text", "integer"],
                "primary_keys": [1],
                "foreign_keys": [],
            }
        ],
    )
    _write_split(
        data_root=data_root,
        split="train",
        examples=[
            {
                "db_id": "train_db",
                "question": "List all train names.",
                "SQL": "SELECT name FROM train_table",
                "evidence": "Use the name column",
            }
        ],
        tables=[
            {
                "db_id": "train_db",
                "table_names": ["train_table"],
                "table_names_original": ["train_table"],
                "column_names": [[-1, "*"], [0, "name"]],
                "column_names_original": [[-1, "*"], [0, "name"]],
                "column_types": ["text", "text"],
                "primary_keys": [],
                "foreign_keys": [],
            }
        ],
    )
    return data_root


def make_company_schema() -> NormalizedSchema:
    """Return a multi-table schema used by rule-level mutation tests."""

    return _schema_from_tables(
        {
            "employees": [
                ("id", "integer"),
                ("name", "text"),
                ("salary", "integer"),
                ("department_id", "integer"),
                ("manager_id", "integer"),
                ("office_id", "integer"),
                ("email", "text"),
            ],
            "departments": [
                ("id", "integer"),
                ("name", "text"),
                ("region", "text"),
                ("budget", "integer"),
                ("manager_id", "integer"),
            ],
            "projects": [
                ("id", "integer"),
                ("department_id", "integer"),
                ("name", "text"),
                ("budget", "integer"),
            ],
            "offices": [
                ("id", "integer"),
                ("city", "text"),
            ],
        },
        primary_keys=[1, 8, 14, 19],
        foreign_keys=[(4, 8), (6, 19), (15, 8)],
    )


def make_single_table_schema() -> NormalizedSchema:
    """Return a single-table schema for minimal mutation fixtures."""

    return _schema_from_tables(
        {
            "employees": [
                ("id", "integer"),
                ("name", "text"),
                ("salary", "integer"),
                ("manager_id", "integer"),
            ]
        },
        primary_keys=[1],
        foreign_keys=[],
    )


def make_normalized_example(
    sql: str,
    *,
    schema: NormalizedSchema | None = None,
    example_id: str = "bird:dev:000000",
    benchmark: str = "bird",
    subset: str = "dev",
    source_id: str | None = "fixture-1",
    db_id: str = "company",
    question: str = "fixture question",
    sqlite_path: str = "/tmp/company.sqlite",
    sqlite_relpath: str = "dev/dev_databases/company/company.sqlite",
    metadata: dict[str, object] | None = None,
) -> NormalizedExample:
    """Build a normalized example with sensible fixture defaults."""

    return NormalizedExample(
        example_id=example_id,
        benchmark=benchmark,
        subset=subset,
        source_id=source_id,
        db_id=db_id,
        question=question,
        gold_sql=sql,
        sqlite_path=sqlite_path,
        sqlite_relpath=sqlite_relpath,
        schema=schema or make_company_schema(),
        metadata=dict(metadata or {}),
    )


def write_normalized_examples(path: Path, examples: list[NormalizedExample]) -> Path:
    """Persist normalized examples as JSONL for pipeline tests."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for example in examples:
            file.write(json.dumps(example.to_dict(), ensure_ascii=True))
            file.write("\n")
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into dictionaries for assertions."""

    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_config(
    path: Path,
    *,
    data_root: Path,
    output_dir: Path,
    benchmark: str = "bird",
    bird_mode: str | None = "dev",
    normalize: bool = True,
    mutate: bool = False,
    equivalence: bool = False,
    execute: bool = False,
    counterexamples: bool = False,
    resume: bool = True,
    overwrite: bool = False,
    sqlite_seconds: int = 120,
    verieql_path: Path | None = None,
    verieql_python: Path | None = None,
    sqldriller_path: Path | None = None,
    sqldriller_python: Path | None = None,
) -> Path:
    """Write a minimal experiment config tailored for integration tests."""

    bird_mode_line = f"bird_mode: {bird_mode}\n" if bird_mode is not None else ""
    verieql_path_value = str(verieql_path) if verieql_path is not None else "null"
    verieql_python_value = (
        str(verieql_python) if verieql_python is not None else "null"
    )
    sqldriller_path_value = (
        str(sqldriller_path) if sqldriller_path is not None else "null"
    )
    sqldriller_python_value = (
        str(sqldriller_python) if sqldriller_python is not None else "null"
    )
    path.write_text(
        f"""
experiment_name: test-run
benchmark: {benchmark}
{bird_mode_line}data_root: {data_root}
output_dir: {output_dir}
resume: {str(resume).lower()}
overwrite: {str(overwrite).lower()}
stages:
  normalize: {str(normalize).lower()}
  mutate: {str(mutate).lower()}
  equivalence: {str(equivalence).lower()}
  execute: {str(execute).lower()}
  counterexamples: {str(counterexamples).lower()}
  augment: false
  report: false

timeouts:
  sqlite_seconds: {sqlite_seconds}

tools:
  verieql_path: {verieql_path_value}
  verieql_python: {verieql_python_value}
  sqldriller_path: {sqldriller_path_value}
  sqldriller_python: {sqldriller_python_value}
""",
        encoding="utf-8",
    )
    return path


def _write_split(
    *,
    data_root: Path,
    split: str,
    examples: list[dict[str, object]],
    tables: list[dict[str, object]],
) -> None:
    """Write one BIRD split and its extracted SQLite placeholder files."""

    split_dir = data_root / split
    split_dir.mkdir(parents=True, exist_ok=True)

    (split_dir / f"{split}.json").write_text(json.dumps(examples), encoding="utf-8")
    (split_dir / f"{split}_tables.json").write_text(
        json.dumps(tables), encoding="utf-8"
    )

    database_dir = split_dir / f"{split}_databases"
    for table in tables:
        db_id = str(table["db_id"])
        db_path = database_dir / db_id
        db_path.mkdir(parents=True, exist_ok=True)
        (db_path / f"{db_id}.sqlite").write_bytes(b"")


def _schema_from_tables(
    tables: dict[str, list[tuple[str, str]]],
    *,
    primary_keys: list[int | tuple[int, ...]],
    foreign_keys: list[tuple[int, int]],
) -> NormalizedSchema:
    """Construct a ``NormalizedSchema`` from compact table definitions."""

    table_names = list(tables)
    column_names: list[tuple[int, str]] = [(-1, "*")]
    column_names_original: list[tuple[int, str]] = [(-1, "*")]
    column_types: list[str] = ["text"]

    for table_index, (_table_name, columns) in enumerate(tables.items()):
        for column_name, column_type in columns:
            column_names.append((table_index, column_name))
            column_names_original.append((table_index, column_name))
            column_types.append(column_type)

    return NormalizedSchema(
        table_names=table_names,
        table_names_original=table_names.copy(),
        column_names=column_names,
        column_names_original=column_names_original,
        column_types=column_types,
        primary_keys=primary_keys,
        foreign_keys=foreign_keys,
    )
