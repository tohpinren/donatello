from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from donatello.benchmarks import BenchmarkError, normalize_examples
from donatello.config import BirdMode, ExperimentConfig, StageConfig
from tests.helpers import write_bird_fixture


def test_bird_normalizes_dev_examples(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)

    examples = normalize_examples(_config(data_root, tmp_path / "out", mode="dev"))

    assert len(examples) == 1
    example = examples[0]
    assert example.example_id == "bird:dev:000000"
    assert example.source_id == "7"
    assert example.db_id == "dev_db"
    assert example.question == "How many rows are in dev_table?"
    assert example.gold_sql == "SELECT COUNT(*) FROM dev_table"
    assert example.sqlite_relpath == "dev/dev_databases/dev_db/dev_db.sqlite"
    assert example.schema.to_dict() == {
        "table_names": ["dev_table"],
        "table_names_original": ["dev_table"],
        "column_names": [(-1, "*"), (0, "id")],
        "column_names_original": [(-1, "*"), (0, "id")],
        "column_types": ["text", "integer"],
        "primary_keys": [1],
        "foreign_keys": [],
    }
    assert example.metadata == {
        "question_id": 7,
        "evidence": "Use COUNT(*)",
        "difficulty": "simple",
    }


def test_bird_normalizes_train_examples(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)

    examples = normalize_examples(_config(data_root, tmp_path / "out", mode="train"))

    assert len(examples) == 1
    example = examples[0]
    assert example.example_id == "bird:train:000000"
    assert example.source_id is None
    assert example.metadata == {"evidence": "Use the name column"}


def test_bird_normalizes_full_mode_in_split_order(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)

    examples = normalize_examples(_config(data_root, tmp_path / "out", mode="full"))

    assert [example.example_id for example in examples] == [
        "bird:dev:000000",
        "bird:train:000000",
    ]


def test_bird_supports_composite_primary_keys(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    tables_path = data_root / "dev" / "dev_tables.json"
    tables = json.loads(tables_path.read_text(encoding="utf-8"))
    tables[0]["primary_keys"] = [1, [2, 3]]
    tables_path.write_text(json.dumps(tables), encoding="utf-8")

    examples = normalize_examples(_config(data_root, tmp_path / "out", mode="dev"))

    assert examples[0].schema.primary_keys == [1, (2, 3)]


def test_bird_missing_tables_file_fails_clearly(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    (data_root / "dev" / "dev_tables.json").unlink()

    with pytest.raises(BenchmarkError, match="Required BIRD file does not exist"):
        normalize_examples(_config(data_root, tmp_path / "out", mode="dev"))


def test_bird_missing_schema_entry_fails_clearly(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    (data_root / "dev" / "dev_tables.json").write_text("[]", encoding="utf-8")

    with pytest.raises(BenchmarkError, match="unknown db_id"):
        normalize_examples(_config(data_root, tmp_path / "out", mode="dev"))


def test_bird_missing_sqlite_file_fails_clearly(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    sqlite_path = data_root / "dev" / "dev_databases" / "dev_db" / "dev_db.sqlite"
    sqlite_path.unlink()

    with pytest.raises(BenchmarkError, match="Missing SQLite database"):
        normalize_examples(_config(data_root, tmp_path / "out", mode="dev"))


def _config(data_root: Path, output_dir: Path, *, mode: str) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_name="bird-test",
        benchmark="bird",
        bird_mode=cast(BirdMode, mode),
        data_root=data_root,
        output_dir=output_dir,
        stages=StageConfig(
            normalize=True,
            mutate=False,
            equivalence=False,
            execute=False,
            counterexamples=False,
            augment=False,
            report=False,
        ),
    )
