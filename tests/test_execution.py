from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import donatello.execution as execution
from donatello.artifacts import artifacts_for
from donatello.config import ExperimentConfig, StageConfig, TimeoutConfig
from donatello.equivalence import write_equivalence_records
from donatello.execution import (
    read_execution_records,
    run_sqlite_execution,
    write_execution_records,
)
from donatello.models import (
    EquivalenceOutcome,
    EquivalenceRecord,
    ExecutionRecord,
    MutationRecord,
)
from donatello.mutation.generator import write_mutation_records
from donatello.result_comparison import ResultSemantics


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


def test_sqlite_execution_distinguishes_bag_from_set_semantics(
    tmp_path: Path,
) -> None:
    db_path = _write_duplicate_company_db(tmp_path / "company.sqlite")
    outcomes: dict[str, str] = {}

    for result_semantics in ("bag", "set"):
        run_root = tmp_path / result_semantics
        artifacts = artifacts_for(run_root / "outputs")
        artifacts.ensure_base_dirs()
        write_mutation_records(
            artifacts.mutants,
            [
                _mutation(
                    "distinct",
                    db_path=db_path,
                    gold_sql="SELECT name FROM employees",
                    mutant_sql="SELECT DISTINCT name FROM employees",
                )
            ],
        )
        write_equivalence_records(
            artifacts.equivalence,
            [_equivalence("distinct", "proven_non_equivalent")],
        )

        run_sqlite_execution(
            _config(run_root, result_semantics=result_semantics),
            artifacts=artifacts,
        )
        [record] = read_execution_records(artifacts.execution)
        outcomes[result_semantics] = record.outcome
        assert record.result_semantics == result_semantics

    assert outcomes == {"bag": "killed", "set": "survived"}


def test_sqlite_execution_remaps_stale_sqlite_paths_via_data_root(
    tmp_path: Path,
) -> None:
    actual_db_path = _write_company_db(tmp_path / "bird" / "company.sqlite")
    stale_db_path = tmp_path / "old-host" / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation(
                "remapped",
                db_path=stale_db_path,
                mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
            )
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence("remapped", "proven_non_equivalent")],
    )

    summary = run_sqlite_execution(_config(tmp_path), artifacts=artifacts)

    assert actual_db_path.exists()
    assert summary.executed_count == 1
    assert summary.killed_count == 1
    rows = read_execution_records(artifacts.execution)
    assert [row.mutant_id for row in rows] == ["remapped"]
    assert rows[0].outcome == "killed"


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


def test_execute_sql_opens_read_only_db_with_immutable_uri(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "company.sqlite"
    db_path.write_text("sqlite", encoding="utf-8")
    seen: dict[str, object] = {}

    class _FakeCursor:
        def fetchall(self) -> list[tuple[int]]:
            return [(1,)]

    class _FakeConnection:
        def set_progress_handler(self, handler: object, steps: int) -> None:
            seen["progress_handler"] = (handler is not None, steps)

        def execute(self, sql: str) -> _FakeCursor:
            seen["sql"] = sql
            return _FakeCursor()

        def close(self) -> None:
            seen["closed"] = True

    def fake_connect(target: str, *, uri: bool) -> _FakeConnection:
        seen["target"] = target
        seen["uri"] = uri
        return _FakeConnection()

    monkeypatch.setattr(execution.sqlite3, "connect", fake_connect)

    rows = execution._execute_sql(
        db_path,
        "SELECT 1",
        timeout_seconds=120,
    )

    assert rows == [(1,)]
    assert seen["uri"] is True
    assert seen["target"] == f"file:{db_path}?mode=ro&immutable=1"
    assert seen["closed"] is True


def test_sqlite_execution_reuses_cached_gold_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation("slow_1", db_path=db_path),
            _mutation("slow_2", db_path=db_path),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            _equivalence("slow_1", "proven_non_equivalent"),
            _equivalence("slow_2", "proven_non_equivalent"),
        ],
    )
    calls: list[str] = []

    def fake_execute_sql(
        path: Path,
        sql: str,
        *,
        timeout_seconds: int,
    ) -> list[tuple[Any, ...]]:
        calls.append(sql)
        raise TimeoutError("SQLite query timed out after 1 seconds.")

    monkeypatch.setattr(execution, "_execute_sql", fake_execute_sql)

    summary = run_sqlite_execution(
        _config(tmp_path, sqlite_seconds=1),
        artifacts=artifacts,
    )

    assert summary.executed_count == 2
    assert summary.timeout_count == 2
    assert calls == ["SELECT name FROM employees ORDER BY id"]
    rows = read_execution_records(artifacts.execution)
    assert [row.outcome for row in rows] == ["timeout", "timeout"]
    assert [row.error_phase for row in rows] == ["gold", "gold"]


def test_sqlite_execution_resumes_partial_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation("done", db_path=db_path, mutant_sql="SELECT same"),
            _mutation("pending", db_path=db_path, mutant_sql="SELECT interrupt"),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            _equivalence("done", "proven_non_equivalent"),
            _equivalence("pending", "proven_non_equivalent"),
        ],
    )

    def interrupted_execute_sql(
        path: Path,
        sql: str,
        *,
        timeout_seconds: int,
    ) -> list[tuple[Any, ...]]:
        if sql == "SELECT interrupt":
            raise KeyboardInterrupt
        return [("Ada",)]

    monkeypatch.setattr(execution, "_execute_sql", interrupted_execute_sql)

    with pytest.raises(KeyboardInterrupt):
        run_sqlite_execution(
            _config(tmp_path, resume=True),
            artifacts=artifacts,
        )

    assert [row.mutant_id for row in read_execution_records(artifacts.execution)] == [
        "done"
    ]

    def resumed_execute_sql(
        path: Path,
        sql: str,
        *,
        timeout_seconds: int,
    ) -> list[tuple[Any, ...]]:
        assert sql != "SELECT same"
        if sql == "SELECT interrupt":
            return [("Grace",)]
        return [("Ada",)]

    monkeypatch.setattr(execution, "_execute_sql", resumed_execute_sql)

    summary = run_sqlite_execution(
        _config(tmp_path, resume=True),
        artifacts=artifacts,
    )

    assert summary.resumed is True
    assert summary.executed_count == 2
    rows = read_execution_records(artifacts.execution)
    assert [row.mutant_id for row in rows] == ["done", "pending"]
    assert [row.outcome for row in rows] == ["survived", "killed"]


def test_sqlite_execution_parallelizes_and_preserves_record_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation("first", db_path=db_path, mutant_sql="SELECT first"),
            _mutation("second", db_path=db_path, mutant_sql="SELECT second"),
            _mutation("third", db_path=db_path, mutant_sql="SELECT third"),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            _equivalence("first", "proven_non_equivalent"),
            _equivalence("second", "proven_non_equivalent"),
            _equivalence("third", "proven_non_equivalent"),
        ],
    )

    gold_calls = 0
    active_mutants = 0
    max_active_mutants = 0
    lock = threading.Lock()

    def fake_execute_sql(
        path: Path,
        sql: str,
        *,
        timeout_seconds: int,
    ) -> list[tuple[Any, ...]]:
        nonlocal gold_calls, active_mutants, max_active_mutants
        if sql == "SELECT name FROM employees ORDER BY id":
            with lock:
                gold_calls += 1
            time.sleep(0.01)
            return [("Ada",), ("Grace",)]

        durations = {
            "SELECT first": 0.05,
            "SELECT second": 0.01,
            "SELECT third": 0.0,
        }
        with lock:
            active_mutants += 1
            max_active_mutants = max(max_active_mutants, active_mutants)
        time.sleep(durations[sql])
        with lock:
            active_mutants -= 1
        return {
            "SELECT first": [("Ada",)],
            "SELECT second": [("Grace",)],
            "SELECT third": [("Ada",), ("Grace",)],
        }[sql]

    monkeypatch.setattr(execution, "_execute_sql", fake_execute_sql)

    summary = run_sqlite_execution(
        _config(tmp_path, workers=2),
        artifacts=artifacts,
    )

    assert summary.executed_count == 3
    assert summary.killed_count == 2
    assert summary.survived_count == 1
    assert gold_calls == 1
    assert max_active_mutants == 2
    rows = read_execution_records(artifacts.execution)
    assert [row.mutant_id for row in rows] == ["first", "second", "third"]
    assert [row.outcome for row in rows] == ["killed", "killed", "survived"]


def test_sqlite_execution_workers_one_preserves_serial_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation("first", db_path=db_path, mutant_sql="SELECT first"),
            _mutation("second", db_path=db_path, mutant_sql="SELECT second"),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            _equivalence("first", "proven_non_equivalent"),
            _equivalence("second", "proven_non_equivalent"),
        ],
    )

    active_mutants = 0
    max_active_mutants = 0
    lock = threading.Lock()

    def fake_execute_sql(
        path: Path,
        sql: str,
        *,
        timeout_seconds: int,
    ) -> list[tuple[Any, ...]]:
        nonlocal active_mutants, max_active_mutants
        if sql == "SELECT name FROM employees ORDER BY id":
            return [("Ada",), ("Grace",)]
        with lock:
            active_mutants += 1
            max_active_mutants = max(max_active_mutants, active_mutants)
        time.sleep(0.01)
        with lock:
            active_mutants -= 1
        return [("Ada",)]

    monkeypatch.setattr(execution, "_execute_sql", fake_execute_sql)

    summary = run_sqlite_execution(
        _config(tmp_path, workers=1),
        artifacts=artifacts,
    )

    assert summary.executed_count == 2
    assert max_active_mutants == 1


def test_sqlite_execution_shared_gold_baseexception_propagates_to_waiters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "company.sqlite"
    mutation = _mutation("shared", db_path=db_path)
    cache = execution._GoldQueryCache(timeout_seconds=1)
    errors: list[type[BaseException]] = []

    def fake_execute_sql(
        path: Path,
        sql: str,
        *,
        timeout_seconds: int,
    ) -> list[tuple[Any, ...]]:
        time.sleep(0.02)
        raise KeyboardInterrupt

    def run_get() -> None:
        try:
            cache.get(mutation)
        except BaseException as exc:  # noqa: B036
            errors.append(type(exc))

    monkeypatch.setattr(execution, "_execute_sql", fake_execute_sql)

    owner = threading.Thread(target=run_get)
    waiter = threading.Thread(target=run_get)
    owner.start()
    time.sleep(0.005)
    waiter.start()
    owner.join()
    waiter.join()

    assert errors == [KeyboardInterrupt, KeyboardInterrupt]


def test_sqlite_execution_does_not_rewrite_checkpointed_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _write_company_db(tmp_path / "company.sqlite")
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation(
                "m1",
                db_path=db_path,
                mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
            )
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence("m1", "proven_non_equivalent")],
    )

    def fail_rewrite(path: Path, records: list[ExecutionRecord]) -> None:
        raise AssertionError("execution artifact should not be rewritten")

    monkeypatch.setattr(execution, "write_execution_records", fail_rewrite)

    summary = run_sqlite_execution(
        _config(tmp_path, resume=False),
        artifacts=artifacts,
    )

    assert summary.executed_count == 1
    rows = read_execution_records(artifacts.execution)
    assert [row.mutant_id for row in rows] == ["m1"]


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
            result_semantics="bag",
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


def test_sqlite_execution_rejects_resume_with_different_result_semantics(
    tmp_path: Path,
) -> None:
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_execution_records(
        artifacts.execution,
        [
            ExecutionRecord(
                mutant_id="m1",
                example_id="bird:dev:000000",
                outcome="survived",
                gold_sql="SELECT 1",
                mutant_sql="SELECT 1",
                sqlite_path="/tmp/example.sqlite",
                rule="c_multi_dist",
                profile="canonical",
                result_semantics="bag",
            )
        ],
    )

    with pytest.raises(ValueError, match="uses result semantics bag"):
        run_sqlite_execution(
            _config(tmp_path, result_semantics="set"),
            artifacts=artifacts,
        )


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


def _write_duplicate_company_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO employees (id, name) VALUES (1, 'Ada'), (2, 'Ada');
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
    workers: int = 1,
    result_semantics: ResultSemantics = "bag",
) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_name="test",
        benchmark="bird",
        bird_mode="dev",
        data_root=tmp_path / "bird",
        output_dir=tmp_path / "outputs",
        result_semantics=result_semantics,
        workers=workers,
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
    gold_sql: str = "SELECT name FROM employees ORDER BY id",
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
        gold_sql=gold_sql,
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
