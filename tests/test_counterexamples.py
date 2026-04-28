from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import sqlmt.counterexamples as counterexamples
from sqlmt.artifacts import artifacts_for
from sqlmt.config import ExperimentConfig, StageConfig, TimeoutConfig, ToolConfig
from sqlmt.equivalence import write_equivalence_records
from sqlmt.execution import write_execution_records
from sqlmt.models import (
    CounterexampleRecord,
    EquivalenceOutcome,
    EquivalenceRecord,
    ExecutionOutcome,
    ExecutionRecord,
    MutationRecord,
)
from sqlmt.mutation.generator import write_mutation_records


def test_counterexample_generation_runs_only_survived_proven_non_equivalent_mutants(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation("run", db_path=db_path),
            _mutation("killed", db_path=db_path),
            _mutation("equivalent", db_path=db_path),
            _mutation("timeout", db_path=db_path),
            _mutation("unsupported", db_path=db_path),
            _mutation("eq_error", db_path=db_path),
            _mutation("exec_timeout", db_path=db_path),
            _mutation("exec_error", db_path=db_path),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            _equivalence("run", "proven_non_equivalent"),
            _equivalence("killed", "proven_non_equivalent"),
            _equivalence("equivalent", "proven_equivalent"),
            _equivalence("timeout", "timeout"),
            _equivalence("unsupported", "unsupported"),
            _equivalence("eq_error", "error"),
            _equivalence("exec_timeout", "proven_non_equivalent"),
            _equivalence("exec_error", "proven_non_equivalent"),
        ],
    )
    write_execution_records(
        artifacts.execution,
        [
            _execution("run", "survived", db_path=db_path),
            _execution("killed", "killed", db_path=db_path),
            _execution("exec_timeout", "timeout", db_path=db_path),
            _execution("exec_error", "error", db_path=db_path),
        ],
    )
    seen: list[str] = []

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        result_path = Path(command[command.index("--result-json") + 1])
        request_path = Path(command[command.index("--request-json") + 1])
        mutant_id = str(
            json.loads(request_path.read_text(encoding="utf-8"))["mutant_id"]
        )
        seen.append(mutant_id)
        result_path.write_text(
            json.dumps({"raw_tag": 0, "counterexample_path": str(cwd / "ce.sqlite")}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    summary = counterexamples.run_counterexample_generation(
        _config(tmp_path),
        artifacts=artifacts,
        runner=fake_runner,
    )

    assert summary.selected_count == 1
    assert summary.generated_count == 1
    assert summary.not_generated_count == 0
    assert summary.timeout_count == 0
    assert summary.error_count == 0
    assert seen == ["run"]
    rows = counterexamples.read_counterexample_records(artifacts.counterexamples)
    assert [row.mutant_id for row in rows] == ["run"]
    assert rows[0].outcome == "generated"


def test_counterexample_generation_maps_outcomes(tmp_path: Path) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation("generated", db_path=db_path),
            _mutation("equivalent", db_path=db_path),
            _mutation("unknown", db_path=db_path),
            _mutation("timeout", db_path=db_path),
            _mutation("failure", db_path=db_path),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence(mutant_id, "proven_non_equivalent") for mutant_id in [
            "generated",
            "equivalent",
            "unknown",
            "timeout",
            "failure",
        ]],
    )
    write_execution_records(
        artifacts.execution,
        [_execution(mutant_id, "survived", db_path=db_path) for mutant_id in [
            "generated",
            "equivalent",
            "unknown",
            "timeout",
            "failure",
        ]],
    )

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        request_path = Path(command[command.index("--request-json") + 1])
        result_path = Path(command[command.index("--result-json") + 1])
        mutant_id = str(
            json.loads(request_path.read_text(encoding="utf-8"))["mutant_id"]
        )
        if mutant_id == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        if mutant_id == "failure":
            raise subprocess.CalledProcessError(1, command, stderr="adapter failed")

        payload = {
            "generated": {"raw_tag": 0, "counterexample_path": str(cwd / "ce.sqlite")},
            "equivalent": {"raw_tag": 1, "counterexample_path": None},
            "unknown": {"raw_tag": -1, "counterexample_path": None},
        }[mutant_id]
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    summary = counterexamples.run_counterexample_generation(
        _config(tmp_path),
        artifacts=artifacts,
        runner=fake_runner,
    )

    assert summary.selected_count == 5
    assert summary.generated_count == 1
    assert summary.not_generated_count == 2
    assert summary.timeout_count == 1
    assert summary.error_count == 1
    rows = {
        row.mutant_id: row
        for row in counterexamples.read_counterexample_records(
            artifacts.counterexamples
        )
    }
    assert rows["generated"].outcome == "generated"
    assert rows["generated"].counterexample_path is not None
    assert rows["equivalent"].outcome == "not_generated"
    assert rows["unknown"].outcome == "not_generated"
    assert rows["timeout"].outcome == "timeout"
    assert rows["failure"].outcome == "error"
    assert rows["failure"].error == "adapter failed"


def test_counterexample_generation_resumes_partial_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation("done", db_path=db_path),
            _mutation("pending", db_path=db_path),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            _equivalence("done", "proven_non_equivalent"),
            _equivalence("pending", "proven_non_equivalent"),
        ],
    )
    write_execution_records(
        artifacts.execution,
        [
            _execution("done", "survived", db_path=db_path),
            _execution("pending", "survived", db_path=db_path),
        ],
    )

    def interrupted_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        request_path = Path(command[command.index("--request-json") + 1])
        result_path = Path(command[command.index("--result-json") + 1])
        mutant_id = str(
            json.loads(request_path.read_text(encoding="utf-8"))["mutant_id"]
        )
        if mutant_id == "pending":
            raise KeyboardInterrupt
        result_path.write_text(
            json.dumps({"raw_tag": 1, "counterexample_path": None}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(KeyboardInterrupt):
        counterexamples.run_counterexample_generation(
            _config(tmp_path, resume=True),
            artifacts=artifacts,
            runner=interrupted_runner,
        )

    assert [
        row.mutant_id
        for row in counterexamples.read_counterexample_records(
            artifacts.counterexamples
        )
    ] == ["done"]

    seen: list[str] = []

    def resumed_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        request_path = Path(command[command.index("--request-json") + 1])
        result_path = Path(command[command.index("--result-json") + 1])
        mutant_id = str(
            json.loads(request_path.read_text(encoding="utf-8"))["mutant_id"]
        )
        seen.append(mutant_id)
        result_path.write_text(
            json.dumps({"raw_tag": 0, "counterexample_path": str(cwd / "ce.sqlite")}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    summary = counterexamples.run_counterexample_generation(
        _config(tmp_path, resume=True),
        artifacts=artifacts,
        runner=resumed_runner,
    )

    assert summary.resumed is True
    assert seen == ["pending"]
    rows = counterexamples.read_counterexample_records(artifacts.counterexamples)
    assert [row.mutant_id for row in rows] == ["done", "pending"]


def test_counterexample_generation_overwrites_existing_artifacts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    artifacts.counterexamples_dir.mkdir(parents=True, exist_ok=True)
    stale_dir = artifacts.counterexamples_dir / "stale"
    stale_dir.mkdir()
    stale_db = stale_dir / "ce0.sqlite"
    stale_db.write_text("stale", encoding="utf-8")
    counterexamples.write_counterexample_records(
        artifacts.counterexamples,
        [
            CounterexampleRecord(
                mutant_id="stale",
                example_id="bird:dev:000000",
                outcome="not_generated",
                gold_sql="SELECT 1",
                mutant_sql="SELECT 1",
                source_sqlite_path=str(db_path),
                rule="c_multi_dist",
                profile="canonical",
            )
        ],
    )
    write_mutation_records(artifacts.mutants, [_mutation("fresh", db_path=db_path)])
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence("fresh", "proven_non_equivalent")],
    )
    write_execution_records(
        artifacts.execution,
        [_execution("fresh", "survived", db_path=db_path)],
    )

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        result_path = Path(command[command.index("--result-json") + 1])
        result_path.write_text(
            json.dumps({"raw_tag": 1, "counterexample_path": None}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    summary = counterexamples.run_counterexample_generation(
        _config(tmp_path, resume=False, overwrite=True),
        artifacts=artifacts,
        runner=fake_runner,
    )

    assert summary.resumed is False
    assert not stale_db.exists()
    rows = counterexamples.read_counterexample_records(artifacts.counterexamples)
    assert [row.mutant_id for row in rows] == ["fresh"]


def _config(
    tmp_path: Path,
    *,
    resume: bool = True,
    overwrite: bool = False,
) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_name="test",
        benchmark="bird",
        bird_mode="dev",
        data_root=tmp_path / "bird",
        output_dir=tmp_path / "outputs",
        resume=resume,
        overwrite=overwrite,
        timeouts=TimeoutConfig(sqldriller_seconds=30),
        tools=ToolConfig(
            sqldriller_path=Path("/Users/tohpinren/Repos/SQLDriller"),
            sqldriller_python=Path("/tmp/sqldriller-python"),
        ),
        stages=StageConfig(
            normalize=False,
            mutate=False,
            equivalence=False,
            execute=False,
            counterexamples=True,
            augment=False,
            report=False,
        ),
    )


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
        gold_sql="SELECT name FROM employees ORDER BY id",
        mutant_sql="SELECT name FROM employees",
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


def _execution(
    mutant_id: str,
    outcome: ExecutionOutcome,
    *,
    db_path: Path,
) -> ExecutionRecord:
    return ExecutionRecord(
        mutant_id=mutant_id,
        example_id="bird:dev:000000",
        outcome=outcome,
        gold_sql="SELECT name FROM employees ORDER BY id",
        mutant_sql="SELECT name FROM employees",
        sqlite_path=str(db_path),
        rule="c_multi_dist",
        profile="canonical",
    )
