from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from sqlmt.artifacts import artifacts_for
from sqlmt.cli import app
from sqlmt.equivalence import write_equivalence_records
from sqlmt.execution import write_execution_records
from sqlmt.models import EquivalenceOutcome, EquivalenceRecord, ExecutionRecord, MutationRecord
from sqlmt.mutation.generator import write_mutation_records
from tests.helpers import write_bird_fixture, write_config

runner = CliRunner()


def test_cli_run_writes_counterexample_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import sqlmt.counterexamples as counterexamples

    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    db_path = _write_db(tmp_path / "company.sqlite")
    artifacts = artifacts_for(output_dir)
    artifacts.ensure_base_dirs()
    write_mutation_records(artifacts.mutants, [_mutation("m1", db_path=db_path)])
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence("m1", "proven_non_equivalent")],
    )
    write_execution_records(
        artifacts.execution,
        [_execution("m1", "survived", db_path=db_path)],
    )
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=False,
        mutate=False,
        equivalence=False,
        execute=False,
        counterexamples=True,
        resume=False,
        sqldriller_path=Path("/Users/tohpinren/Repos/SQLDriller"),
        sqldriller_python=Path("/tmp/sqldriller-python"),
    )

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        result_path = Path(command[command.index("--result-json") + 1])
        result_path.write_text(
            json.dumps({"raw_tag": 0, "counterexample_path": str(cwd / "ce.sqlite")}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(counterexamples, "_run_subprocess", fake_runner)

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Generated counterexamples for 1 retained survivors" in result.stdout
    assert "1 generated" in result.stdout
    assert output_dir.joinpath("counterexamples.jsonl").exists()


def test_cli_run_counterexamples_only_requires_existing_execution(
    tmp_path: Path,
) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    db_path = _write_db(tmp_path / "company.sqlite")
    artifacts = artifacts_for(output_dir)
    artifacts.ensure_base_dirs()
    write_mutation_records(artifacts.mutants, [_mutation("m1", db_path=db_path)])
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
        execute=False,
        counterexamples=True,
        resume=False,
        sqldriller_path=Path("/Users/tohpinren/Repos/SQLDriller"),
        sqldriller_python=Path("/tmp/sqldriller-python"),
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "execution.jsonl does not exist" in result.stdout + result.stderr


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


def _mutation(mutant_id: str, *, db_path: Path) -> MutationRecord:
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
        mutant_sql="SELECT name FROM employees WHERE id = 1",
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


def _execution(mutant_id: str, outcome: str, *, db_path: Path) -> ExecutionRecord:
    return ExecutionRecord(
        mutant_id=mutant_id,
        example_id="bird:dev:000000",
        outcome=outcome,
        gold_sql="SELECT name FROM employees",
        mutant_sql="SELECT name FROM employees WHERE id = 1",
        sqlite_path=str(db_path),
        rule="c_multi_dist",
        profile="canonical",
    )
