from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from sqlmt.artifacts import artifacts_for
from sqlmt.augmentation import read_augmentation_records, run_augmentation
from sqlmt.counterexamples import write_counterexample_records
from sqlmt.equivalence import write_equivalence_records
from sqlmt.execution import read_execution_records, write_execution_records
from sqlmt.models import (
    CounterexampleRecord,
    EquivalenceRecord,
    ExecutionOutcome,
    ExecutionRecord,
    MutationRecord,
)
from sqlmt.mutation.generator import write_mutation_records


def test_augmentation_merges_counterexample_rows_and_reruns_mutants(
    tmp_path: Path,
) -> None:
    source_db = _write_company_db(tmp_path / "company.sqlite", [(1, "Ada")])
    counterexample_db = _write_company_db(
        tmp_path / "ce.sqlite",
        [(2, "Grace")],
    )
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    _write_base_artifacts(artifacts, source_db, counterexample_db)

    summary = run_augmentation(_config(tmp_path), artifacts=artifacts)

    assert summary.database_count == 1
    assert summary.counterexample_count == 1
    assert summary.inserted_row_count == 1
    assert summary.skipped_row_count == 0
    assert summary.augmented_execution.mutation_score == 1.0

    records = read_augmentation_records(artifacts.augmentation)
    assert len(records) == 1
    augmented_db = Path(records[0].augmented_sqlite_path)
    assert augmented_db.exists()
    assert _employee_rows(source_db) == [(1, "Ada")]
    assert _employee_rows(augmented_db) == [(1, "Ada"), (2, "Grace")]

    execution_rows = read_execution_records(artifacts.augmented_execution)
    assert [(row.mutant_id, row.outcome) for row in execution_rows] == [
        ("m1", "killed")
    ]
    assert execution_rows[0].sqlite_path == str(augmented_db)


def test_augmentation_conservatively_warns_for_duplicates_and_schema_mismatches(
    tmp_path: Path,
) -> None:
    source_db = _write_company_db(tmp_path / "company.sqlite", [(1, "Ada")])
    duplicate_db = _write_company_db(tmp_path / "duplicate.sqlite", [(1, "Ada")])
    mismatch_db = tmp_path / "mismatch.sqlite"
    connection = sqlite3.connect(mismatch_db)
    try:
        connection.executescript(
            """
            CREATE TABLE missing_table (id INTEGER);
            INSERT INTO missing_table (id) VALUES (1);
            CREATE TABLE employees (id INTEGER, name TEXT, unknown TEXT);
            INSERT INTO employees (id, name, unknown) VALUES (2, 'Grace', 'x');
            """
        )
    finally:
        connection.close()

    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    _write_base_artifacts(
        artifacts,
        source_db,
        duplicate_db,
        extra_counterexamples=[
            _counterexample("m2", source_db, mismatch_db),
        ],
    )

    summary = run_augmentation(_config(tmp_path), artifacts=artifacts)

    assert summary.inserted_row_count == 0
    assert summary.skipped_row_count == 3
    records = read_augmentation_records(artifacts.augmentation)
    warnings = "\n".join(warning for record in records for warning in record.warnings)
    assert "duplicate row skipped" in warnings
    assert "missing table skipped" in warnings
    assert "column mismatch skipped" in warnings


def test_augmentation_reruns_successful_mutants_when_one_counterexample_fails(
    tmp_path: Path,
) -> None:
    source_db = _write_company_db(tmp_path / "company.sqlite", [(1, "Ada")])
    counterexample_db = _write_company_db(tmp_path / "ce.sqlite", [(2, "Grace")])
    broken_db = tmp_path / "broken.sqlite"
    broken_db.write_text("not sqlite", encoding="utf-8")
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    _write_base_artifacts(
        artifacts,
        source_db,
        counterexample_db,
        extra_counterexamples=[
            _counterexample("m2", source_db, broken_db),
        ],
    )

    summary = run_augmentation(_config(tmp_path), artifacts=artifacts)

    assert summary.error_count == 1
    rows = read_execution_records(artifacts.augmented_execution)
    assert [(row.mutant_id, row.outcome) for row in rows] == [("m1", "killed")]
    records = read_augmentation_records(artifacts.augmentation)
    assert records[0].mutant_ids == ["m1", "m2"]
    assert records[0].merged_mutant_ids == ["m1"]


def test_augmentation_resumes_existing_artifacts(tmp_path: Path) -> None:
    source_db = _write_company_db(tmp_path / "company.sqlite", [(1, "Ada")])
    counterexample_db = _write_company_db(tmp_path / "ce.sqlite", [(2, "Grace")])
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    _write_base_artifacts(artifacts, source_db, counterexample_db)

    first = run_augmentation(_config(tmp_path), artifacts=artifacts)
    second = run_augmentation(_config(tmp_path), artifacts=artifacts)

    assert first.resumed is False
    assert second.resumed is True
    assert second.database_count == 1
    assert second.augmented_execution.executed_count == 1


def test_augmentation_existing_artifacts_require_resume_or_overwrite(
    tmp_path: Path,
) -> None:
    source_db = _write_company_db(tmp_path / "company.sqlite", [(1, "Ada")])
    counterexample_db = _write_company_db(tmp_path / "ce.sqlite", [(2, "Grace")])
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    _write_base_artifacts(artifacts, source_db, counterexample_db)
    run_augmentation(_config(tmp_path), artifacts=artifacts)

    with pytest.raises(ValueError, match="Augmentation outputs already exist"):
        run_augmentation(
            _config(tmp_path, resume=False, overwrite=False),
            artifacts=artifacts,
        )

    summary = run_augmentation(
        _config(tmp_path, resume=False, overwrite=True),
        artifacts=artifacts,
    )
    assert summary.resumed is False
    assert summary.database_count == 1


def _write_base_artifacts(
    artifacts,
    source_db: Path,
    counterexample_db: Path,
    *,
    extra_counterexamples: list[CounterexampleRecord] | None = None,
) -> None:
    write_mutation_records(
        artifacts.mutants,
        [_mutation("m1", source_db), _mutation("m2", source_db)],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence("m1"), _equivalence("m2")],
    )
    write_execution_records(
        artifacts.execution,
        [
            _execution("m1", source_db, "survived"),
            _execution("m2", source_db, "survived"),
        ],
    )
    records = [_counterexample("m1", source_db, counterexample_db)]
    records.extend(extra_counterexamples or [])
    write_counterexample_records(artifacts.counterexamples, records)


def _write_company_db(path: Path, rows: list[tuple[int, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT)")
        connection.executemany("INSERT INTO employees (id, name) VALUES (?, ?)", rows)
        connection.commit()
    finally:
        connection.close()
    return path


def _employee_rows(path: Path) -> list[tuple[int, str]]:
    connection = sqlite3.connect(path)
    try:
        return list(connection.execute("SELECT id, name FROM employees ORDER BY id"))
    finally:
        connection.close()


def _mutation(mutant_id: str, db_path: Path) -> MutationRecord:
    return MutationRecord(
        mutant_id=mutant_id,
        example_id=f"bird:dev:{mutant_id}",
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
        mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
        sqlite_path=str(db_path),
        sqlite_relpath="company.sqlite",
    )


def _equivalence(mutant_id: str) -> EquivalenceRecord:
    return EquivalenceRecord(
        mutant_id=mutant_id,
        example_id=f"bird:dev:{mutant_id}",
        outcome="proven_non_equivalent",
        raw_state=None,
        timeout_seconds=120,
    )


def _execution(
    mutant_id: str,
    db_path: Path,
    outcome: str,
) -> ExecutionRecord:
    return ExecutionRecord(
        mutant_id=mutant_id,
        example_id=f"bird:dev:{mutant_id}",
        outcome=cast(ExecutionOutcome, outcome),
        gold_sql="SELECT name FROM employees ORDER BY id",
        mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
        sqlite_path=str(db_path),
        rule="c_multi_dist",
        profile="canonical",
    )


def _counterexample(
    mutant_id: str,
    source_db: Path,
    counterexample_db: Path,
) -> CounterexampleRecord:
    return CounterexampleRecord(
        mutant_id=mutant_id,
        example_id=f"bird:dev:{mutant_id}",
        outcome="generated",
        gold_sql="SELECT name FROM employees ORDER BY id",
        mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
        source_sqlite_path=str(source_db),
        rule="c_multi_dist",
        profile="canonical",
        counterexample_path=str(counterexample_db),
        raw_tag=0,
        metadata={"db_id": "company", "benchmark": "bird"},
    )


def _config(
    tmp_path: Path,
    *,
    resume: bool = True,
    overwrite: bool = False,
):
    from sqlmt.config import ExperimentConfig, StageConfig

    return ExperimentConfig(
        experiment_name="test",
        benchmark="bird",
        bird_mode="dev",
        data_root=tmp_path / "bird",
        output_dir=tmp_path / "outputs",
        resume=resume,
        overwrite=overwrite,
        stages=StageConfig(
            normalize=False,
            mutate=False,
            equivalence=False,
            execute=False,
            counterexamples=False,
            augment=True,
            report=False,
        ),
    )
