from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from donatello.artifacts import artifacts_for
from donatello.cli import app
from donatello.counterexamples import write_counterexample_records
from donatello.equivalence import write_equivalence_records
from donatello.execution import write_execution_records
from donatello.models import CounterexampleRecord, EquivalenceRecord, ExecutionRecord
from donatello.mutation.generator import write_mutation_records
from tests.helpers import write_bird_fixture, write_config
from tests.test_augmentation import _mutation

runner = CliRunner()


def test_cli_run_writes_augmentation_and_reports(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    source_db = _write_db(tmp_path / "company.sqlite", [(1, "Ada")])
    counterexample_db = _write_db(tmp_path / "ce.sqlite", [(2, "Grace")])
    output_dir = tmp_path / "outputs" / "bird-dev"
    artifacts = artifacts_for(output_dir)
    artifacts.ensure_base_dirs()
    write_mutation_records(artifacts.mutants, [_mutation("m1", source_db)])
    write_equivalence_records(
        artifacts.equivalence,
        [
            EquivalenceRecord(
                mutant_id="m1",
                example_id="bird:dev:m1",
                outcome="proven_non_equivalent",
                raw_state=None,
                timeout_seconds=120,
            )
        ],
    )
    write_execution_records(
        artifacts.execution,
        [
            ExecutionRecord(
                mutant_id="m1",
                example_id="bird:dev:m1",
                outcome="survived",
                gold_sql="SELECT name FROM employees ORDER BY id",
                mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
                sqlite_path=str(source_db),
                rule="c_multi_dist",
                profile="canonical",
            )
        ],
    )
    write_counterexample_records(
        artifacts.counterexamples,
        [
            CounterexampleRecord(
                mutant_id="m1",
                example_id="bird:dev:m1",
                outcome="generated",
                gold_sql="SELECT name FROM employees ORDER BY id",
                mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
                source_sqlite_path=str(source_db),
                rule="c_multi_dist",
                profile="canonical",
                counterexample_path=str(counterexample_db),
                raw_tag=0,
                metadata={"db_id": "company"},
            )
        ],
    )
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=False,
        mutate=False,
        equivalence=False,
        execute=False,
        counterexamples=False,
        augment=True,
        report=True,
        resume=False,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Augmented 1 databases from 1 generated counterexamples" in result.stdout
    assert "mutation score 1.0000" in result.stdout
    assert "Wrote report:" in result.stdout
    assert output_dir.joinpath("augmented_databases", "augmentation.jsonl").exists()
    assert output_dir.joinpath("execution_augmented.jsonl").exists()
    assert output_dir.joinpath("report.md").exists()
    assert output_dir.joinpath("report.json").exists()


def test_cli_run_augment_only_requires_counterexamples(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    artifacts = artifacts_for(output_dir)
    artifacts.ensure_base_dirs()
    source_db = _write_db(tmp_path / "company.sqlite", [(1, "Ada")])
    write_mutation_records(artifacts.mutants, [_mutation("m1", source_db)])
    write_equivalence_records(
        artifacts.equivalence,
        [
            EquivalenceRecord(
                mutant_id="m1",
                example_id="bird:dev:m1",
                outcome="proven_non_equivalent",
                raw_state=None,
                timeout_seconds=120,
            )
        ],
    )
    write_execution_records(
        artifacts.execution,
        [
            ExecutionRecord(
                mutant_id="m1",
                example_id="bird:dev:m1",
                outcome="survived",
                gold_sql="SELECT name FROM employees ORDER BY id",
                mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
                sqlite_path=str(source_db),
                rule="c_multi_dist",
                profile="canonical",
            )
        ],
    )
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=False,
        mutate=False,
        equivalence=False,
        execute=False,
        counterexamples=False,
        augment=True,
        resume=False,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "counterexamples.jsonl does not exist" in result.stdout + result.stderr


def _write_db(path: Path, rows: list[tuple[int, str]]) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT)")
        connection.executemany("INSERT INTO employees (id, name) VALUES (?, ?)", rows)
        connection.commit()
    finally:
        connection.close()
    return path
