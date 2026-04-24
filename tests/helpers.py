from __future__ import annotations

import json
from pathlib import Path


def write_bird_fixture(root: Path) -> Path:
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


def write_config(
    path: Path,
    *,
    data_root: Path,
    output_dir: Path,
    benchmark: str = "bird",
    bird_mode: str = "dev",
    normalize: bool = True,
    mutate: bool = False,
) -> Path:
    path.write_text(
        f"""
experiment_name: test-run
benchmark: {benchmark}
bird_mode: {bird_mode}
data_root: {data_root}
output_dir: {output_dir}
stages:
  normalize: {str(normalize).lower()}
  mutate: {str(mutate).lower()}
  equivalence: false
  execute: false
  counterexamples: false
  augment: false
  report: false
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
