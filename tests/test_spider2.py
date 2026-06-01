from __future__ import annotations

from pathlib import Path

import pytest

from donatello.benchmarks import BenchmarkError, normalize_examples
from donatello.benchmarks import spider2 as spider2_benchmark
from donatello.config import ExperimentConfig, StageConfig
from tests.helpers import write_spider2_fixture


def test_spider2_normalizes_expected_local_subset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = write_spider2_fixture(tmp_path)
    monkeypatch.setattr(
        spider2_benchmark,
        "EXPECTED_INSTANCE_IDS",
        ["local003", "local004"],
    )

    examples = normalize_examples(_config(data_root, tmp_path / "out"))

    assert [example.example_id for example in examples] == [
        "spider2:dev:local003",
        "spider2:dev:local004",
    ]
    assert [example.source_id for example in examples] == ["local003", "local004"]
    assert {example.db_id for example in examples} == {"RetailOps"}
    assert examples[0].question == "Show every customer name."
    assert (
        examples[0].gold_sql
        == "SELECT customer_name FROM customers ORDER BY customer_id"
    )
    assert examples[0].sqlite_relpath == "local_sqlite/RetailOps.sqlite"
    assert examples[0].metadata == {
        "instance_id": "local003",
        "db": "RetailOps",
        "external_knowledge": "retail.md",
        "original_question": "Show every customer name.",
    }
    assert examples[0].schema.to_dict() == {
        "table_names": ["customers", "orders"],
        "table_names_original": ["customers", "orders"],
        "column_names": [
            (-1, "*"),
            (0, "customer_id"),
            (0, "customer_name"),
            (1, "order_id"),
            (1, "customer_id"),
            (1, "total"),
        ],
        "column_names_original": [
            (-1, "*"),
            (0, "customer_id"),
            (0, "customer_name"),
            (1, "order_id"),
            (1, "customer_id"),
            (1, "total"),
        ],
        "column_types": ["text", "integer", "text", "integer", "integer", "real"],
        "primary_keys": [1, 3],
        "foreign_keys": [(4, 1)],
    }


def test_spider2_fails_when_expected_subset_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = write_spider2_fixture(tmp_path)
    monkeypatch.setattr(
        spider2_benchmark,
        "EXPECTED_INSTANCE_IDS",
        ["local003", "local004", "local999"],
    )

    with pytest.raises(BenchmarkError, match="missing expected ids: local999"):
        normalize_examples(_config(data_root, tmp_path / "out"))


def _config(data_root: Path, output_dir: Path) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_name="spider2-test",
        benchmark="spider2",
        bird_mode=None,
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
