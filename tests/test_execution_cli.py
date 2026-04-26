from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from sqlmt.artifacts import artifacts_for
from sqlmt.cli import app
from sqlmt.equivalence import write_equivalence_records
from sqlmt.models import EquivalenceOutcome, EquivalenceRecord, MutationRecord
from sqlmt.mutation.generator import write_mutation_records
from tests.helpers import write_bird_fixture, write_config

runner = CliRunner()


def test_cli_run_writes_execution_output(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    db_path = _write_db(tmp_path / "company.sqlite")
    artifacts = artifacts_for(output_dir)
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation(
                "m1",
                db_path=db_path,
                mutant_sql="SELECT name FROM employees WHERE id = 1",
            )
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence("m1", "proven_non_equivalent")],
    )
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=False,
        mutate=False,
        equivalence=False,
        execute=True,
        resume=False,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Executed 1 retained mutants" in result.stdout
    assert "0 timeout" in result.stdout
    assert "mutation score 1.0000" in result.stdout
    assert output_dir.joinpath("execution.jsonl").exists()


def test_cli_run_execute_only_requires_existing_equivalence(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    artifacts = artifacts_for(output_dir)
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [_mutation("m1", db_path=tmp_path / "company.sqlite")],
    )
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=False,
        mutate=False,
        equivalence=False,
        execute=True,
        resume=False,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "equivalence.jsonl does not exist" in result.stdout + result.stderr


def _write_db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO employees (id, name) VALUES (1, 'Ada'), (2, 'Grace');
            """
        )
    finally:
        connection.close()
    return path


def _mutation(
    mutant_id: str,
    *,
    db_path: Path,
    mutant_sql: str = "SELECT name FROM employees",
) -> MutationRecord:
    return MutationRecord(
        mutant_id=mutant_id,
        example_id="bird:dev:000000",
        benchmark="bird",
        subset="dev",
        db_id="company",
        source_id="fixture-1",
        question="fixture question",
        rule="c_multi_dist",
        profile="canonical",
        status="generated",
        skip_reason=None,
        gold_sql="SELECT name FROM employees",
        mutant_sql=mutant_sql,
        sqlite_path=str(db_path),
        sqlite_relpath="company.sqlite",
    )


def _equivalence(mutant_id: str, outcome: EquivalenceOutcome) -> EquivalenceRecord:
    return EquivalenceRecord(
        mutant_id=mutant_id,
        example_id="bird:dev:000000",
        outcome=outcome,
        raw_state=None,
        timeout_seconds=120,
    )
