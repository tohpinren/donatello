from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlmt.artifacts import artifacts_for
from sqlmt.config import ExperimentConfig, StageConfig, TimeoutConfig
from sqlmt.equivalence import write_equivalence_records
from sqlmt.execution import (
    read_execution_records,
    run_sqlite_execution,
    write_execution_records,
)
from sqlmt.models import (
    EquivalenceOutcome,
    EquivalenceRecord,
    ExecutionRecord,
    MutationRecord,
)
from sqlmt.mutation.generator import write_mutation_records


def test_sqlite_execution_runs_only_proven_non_equivalent_mutants(
    tmp_path: Path,
) -> None:
    db_path = _write_company_db(tmp_path / "company.sqlite")
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation(
                "run",
                db_path=db_path,
                mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
            ),
            _mutation("equivalent", db_path=db_path),
            _mutation("timeout", db_path=db_path),
            _mutation("unsupported", db_path=db_path),
            _mutation("eq_error", db_path=db_path),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            _equivalence("run", "proven_non_equivalent"),
            _equivalence("equivalent", "proven_equivalent"),
            _equivalence("timeout", "timeout"),
            _equivalence("unsupported", "unsupported"),
            _equivalence("eq_error", "error"),
        ],
    )

    summary = run_sqlite_execution(_config(tmp_path), artifacts=artifacts)

    assert summary.executed_count == 1
    assert summary.killed_count == 1
    assert summary.survived_count == 0
    assert summary.error_count == 0
    assert summary.mutation_score == 1.0
    rows = read_execution_records(artifacts.execution)
    assert [row.mutant_id for row in rows] == ["run"]
    assert rows[0].outcome == "killed"


def test_sqlite_execution_classifies_outcomes_and_scores(
    tmp_path: Path,
) -> None:
    db_path = _write_company_db(tmp_path / "company.sqlite")
    missing_db_path = tmp_path / "missing.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation(
                "killed",
                db_path=db_path,
                mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
            ),
            _mutation("survived", db_path=db_path),
            _mutation("mutant_error", db_path=db_path, mutant_sql="SELECT nope"),
            _mutation("gold_error", db_path=missing_db_path),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            _equivalence("killed", "proven_non_equivalent"),
            _equivalence("survived", "proven_non_equivalent"),
            _equivalence("mutant_error", "proven_non_equivalent"),
            _equivalence("gold_error", "proven_non_equivalent"),
        ],
    )

    summary = run_sqlite_execution(_config(tmp_path), artifacts=artifacts)

    assert summary.executed_count == 4
    assert summary.killed_count == 1
    assert summary.survived_count == 1
    assert summary.timeout_count == 0
    assert summary.error_count == 2
    assert summary.mutation_score == 0.5
    rows = {row.mutant_id: row for row in read_execution_records(artifacts.execution)}
    assert rows["killed"].outcome == "killed"
    assert rows["killed"].gold_row_count == 2
    assert rows["killed"].mutant_row_count == 1
    assert rows["survived"].outcome == "survived"
    assert rows["mutant_error"].outcome == "error"
    assert rows["mutant_error"].error_phase == "mutant"
    assert rows["gold_error"].outcome == "error"
    assert rows["gold_error"].error_phase == "gold"


def test_sqlite_execution_times_out_long_running_query(tmp_path: Path) -> None:
    db_path = _write_company_db(tmp_path / "company.sqlite")
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation(
                "slow",
                db_path=db_path,
                mutant_sql=(
                    "WITH RECURSIVE cnt(x) AS ("
                    "SELECT 1 UNION ALL SELECT x + 1 FROM cnt"
                    ") SELECT x FROM cnt"
                ),
            )
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence("slow", "proven_non_equivalent")],
    )

    summary = run_sqlite_execution(
        _config(tmp_path, sqlite_seconds=1),
        artifacts=artifacts,
    )

    assert summary.executed_count == 1
    assert summary.timeout_count == 1
    assert summary.error_count == 0
    assert summary.mutation_score is None
    rows = read_execution_records(artifacts.execution)
    assert rows[0].outcome == "timeout"
    assert rows[0].error_phase == "mutant"
    assert "timed out" in (rows[0].error or "")


def test_sqlite_execution_resumes_existing_artifact(tmp_path: Path) -> None:
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    existing = [
        ExecutionRecord(
            mutant_id="m1",
            example_id="bird:dev:000000",
            outcome="survived",
            gold_sql="SELECT 1",
            mutant_sql="SELECT 1",
            sqlite_path="/tmp/example.sqlite",
            rule="c_multi_dist",
            profile="canonical",
            gold_row_count=1,
            mutant_row_count=1,
        )
    ]
    write_execution_records(artifacts.execution, existing)

    summary = run_sqlite_execution(_config(tmp_path), artifacts=artifacts)

    assert summary.resumed is True
    assert summary.executed_count == 1
    assert summary.survived_count == 1
    assert read_execution_records(artifacts.execution) == existing


def test_sqlite_execution_overwrites_existing_artifact(tmp_path: Path) -> None:
    db_path = _write_company_db(tmp_path / "company.sqlite")
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_execution_records(
        artifacts.execution,
        [
            ExecutionRecord(
                mutant_id="stale",
                example_id="bird:dev:000000",
                outcome="survived",
                gold_sql="SELECT 1",
                mutant_sql="SELECT 1",
                sqlite_path=str(db_path),
                rule="c_multi_dist",
                profile="canonical",
            )
        ],
    )
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation(
                "fresh",
                db_path=db_path,
                mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
            )
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence("fresh", "proven_non_equivalent")],
    )

    summary = run_sqlite_execution(
        _config(tmp_path, resume=False, overwrite=True),
        artifacts=artifacts,
    )

    assert summary.resumed is False
    assert summary.executed_count == 1
    rows = read_execution_records(artifacts.execution)
    assert [row.mutant_id for row in rows] == ["fresh"]


def _write_company_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _config(
    tmp_path: Path,
    *,
    resume: bool = True,
    overwrite: bool = False,
    sqlite_seconds: int = 120,
) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_name="test",
        benchmark="bird",
        bird_mode="dev",
        data_root=tmp_path / "bird",
        output_dir=tmp_path / "outputs",
        resume=resume,
        overwrite=overwrite,
        timeouts=TimeoutConfig(sqlite_seconds=sqlite_seconds),
        stages=StageConfig(
            normalize=False,
            mutate=False,
            equivalence=False,
            execute=True,
            counterexamples=False,
            augment=False,
            report=False,
        ),
    )


def _mutation(
    mutant_id: str,
    *,
    db_path: Path,
    mutant_sql: str = "SELECT name FROM employees ORDER BY id",
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
        gold_sql="SELECT name FROM employees ORDER BY id",
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
