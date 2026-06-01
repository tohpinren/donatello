from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import donatello.counterexamples as counterexamples
from donatello.artifacts import artifacts_for
from donatello.config import ExperimentConfig, StageConfig, TimeoutConfig, ToolConfig
from donatello.equivalence import write_equivalence_records
from donatello.execution import write_execution_records
from donatello.models import (
    CounterexampleRecord,
    EquivalenceOutcome,
    EquivalenceRecord,
    ExecutionOutcome,
    ExecutionRecord,
    MutationRecord,
)
from donatello.mutation.generator import write_mutation_records


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
        request = json.loads(request_path.read_text(encoding="utf-8"))
        mutant_id = str(request["mutant_id"])
        output_dir = Path(str(request["output_dir"]))
        seen.append(mutant_id)
        counterexample_path = output_dir / "ce.sqlite"
        counterexample_path.write_text("sqlite", encoding="utf-8")
        result_path.write_text(
            json.dumps({"raw_tag": 0, "counterexample_path": str(counterexample_path)}),
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
        request = json.loads(request_path.read_text(encoding="utf-8"))
        mutant_id = str(request["mutant_id"])
        output_dir = Path(str(request["output_dir"]))
        if mutant_id == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        if mutant_id == "failure":
            raise subprocess.CalledProcessError(1, command, stderr="adapter failed")

        generated_path = output_dir / "ce.sqlite"
        generated_path.write_text("sqlite", encoding="utf-8")
        payload = {
            "generated": {"raw_tag": 0, "counterexample_path": str(generated_path)},
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


def test_counterexample_generation_remaps_stale_sqlite_paths_via_data_root(
    tmp_path: Path,
) -> None:
    actual_db_path = tmp_path / "bird" / "company.sqlite"
    actual_db_path.parent.mkdir(parents=True, exist_ok=True)
    actual_db_path.write_text("sqlite", encoding="utf-8")
    stale_db_path = tmp_path / "old-host" / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [_mutation("remapped", db_path=stale_db_path)],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence("remapped", "proven_non_equivalent")],
    )
    write_execution_records(
        artifacts.execution,
        [_execution("remapped", "survived", db_path=stale_db_path)],
    )

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        request_path = Path(command[command.index("--request-json") + 1])
        result_path = Path(command[command.index("--result-json") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        assert request["source_sqlite_path"] == str(actual_db_path.resolve())
        staged_path = Path(str(request["output_dir"])) / "ce.sqlite"
        staged_path.write_text("sqlite", encoding="utf-8")
        result_path.write_text(
            json.dumps({"raw_tag": 0, "counterexample_path": str(staged_path)}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    summary = counterexamples.run_counterexample_generation(
        _config(tmp_path),
        artifacts=artifacts,
        runner=fake_runner,
    )

    assert summary.generated_count == 1
    rows = counterexamples.read_counterexample_records(artifacts.counterexamples)
    assert rows[0].source_sqlite_path == str(actual_db_path.resolve())


def test_counterexample_generation_uses_configured_scratch_root_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "company.sqlite"
    db_path.write_text("sqlite", encoding="utf-8")
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [_mutation("scratch", db_path=db_path)],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [_equivalence("scratch", "proven_non_equivalent")],
    )
    write_execution_records(
        artifacts.execution,
        [_execution("scratch", "survived", db_path=db_path)],
    )
    override = tmp_path / "custom-scratch"
    monkeypatch.setenv("DONATELLO_SQLDRILLER_SCRATCH_ROOT", str(override))

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        request_path = Path(command[command.index("--request-json") + 1])
        result_path = Path(command[command.index("--result-json") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        assert Path(request["scratch_root"]) == override.resolve()
        result_path.write_text(
            json.dumps({"raw_tag": 1, "counterexample_path": None}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    summary = counterexamples.run_counterexample_generation(
        _config(tmp_path),
        artifacts=artifacts,
        runner=fake_runner,
    )

    assert summary.not_generated_count == 1


def test_counterexample_materialization_accepts_resolved_staging_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    staging_dir = tmp_path / "outputs" / "sqldriller-output-abc"
    generated_dir = staging_dir / "mutant-1"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_path = generated_dir / "ce0.sqlite"
    generated_path.write_text("sqlite", encoding="utf-8")
    output_dir = tmp_path / "outputs" / "counterexamples"
    record = CounterexampleRecord(
        mutant_id="mutant-1",
        example_id="bird:dev:000000",
        outcome="generated",
        gold_sql="SELECT 1",
        mutant_sql="SELECT 2",
        source_sqlite_path=str(tmp_path / "company.sqlite"),
        rule="c_multi_dist",
        profile="canonical",
        counterexample_path=str(generated_path.resolve()),
        raw_tag=0,
    )

    materialized = counterexamples._materialize_counterexample_result(
        counterexamples._CounterexampleResult(
            record=record,
            staging_dir=Path("outputs") / "sqldriller-output-abc",
        ),
        output_dir=output_dir,
    )

    assert materialized.outcome == "generated"
    assert materialized.error is None
    assert materialized.counterexample_path is not None
    assert Path(materialized.counterexample_path).exists()
    assert Path(materialized.counterexample_path).is_relative_to(output_dir)


def test_counterexample_adapter_uses_configured_scratch_root(tmp_path: Path) -> None:
    sqldriller_root = tmp_path / "sqldriller"
    utils_dir = sqldriller_root / "utils"
    utils_dir.mkdir(parents=True)
    (utils_dir / "__init__.py").write_text("", encoding="utf-8")
    (utils_dir / "constants.py").write_text(
        "class _Mode:\n"
        "    exec = 'exec'\n\n"
        "sql_equiv_mode = _Mode()\n",
        encoding="utf-8",
    )
    (utils_dir / "utils.py").write_text(
        "import json\n"
        "import os\n"
        "import sqlite3\n"
        "from pathlib import Path\n\n"
        "def check_equivalence(gold_sql, mutant_sql, _unused, schema_db_dir, db_dir, "
        "db_id, mode, benchmark):\n"
        "    sample_db = Path('data') / benchmark / 'schema_for_sample_values'\n"
        "    sample_db = sample_db / db_id / f'{db_id}.sqlite'\n"
        "    schema_db = Path(schema_db_dir) / db_id / f'{db_id}.sqlite'\n"
        "    fuzz_db = Path(db_dir) / db_id / f'{db_id}.sqlite'\n"
        "    for path in [sample_db, schema_db, fuzz_db]:\n"
        "        conn = sqlite3.connect(str(path))\n"
        "        conn.execute('select count(*) from sqlite_master').fetchone()\n"
        "        conn.close()\n"
        "    marker = Path(os.environ['DONATELLO_ADAPTER_MARKER'])\n"
        "    marker.write_text(json.dumps({\n"
        "        'schema_db_dir': schema_db_dir,\n"
        "        'db_dir': db_dir,\n"
        "        'sample_db': str(sample_db.resolve()),\n"
        "        'schema_db': str(schema_db.resolve()),\n"
        "        'fuzz_db': str(fuzz_db.resolve()),\n"
        "        'sample_is_symlink': sample_db.is_symlink(),\n"
        "        'schema_is_symlink': schema_db.is_symlink(),\n"
        "        'fuzz_is_symlink': fuzz_db.is_symlink(),\n"
        "    }), encoding='utf-8')\n"
        "    return 1, None\n\n"
        "def simplify_ce(*args, **kwargs):\n"
        "    return None\n",
        encoding="utf-8",
    )

    source_sqlite = tmp_path / "source.sqlite"
    connection = sqlite3.connect(source_sqlite)
    connection.execute("CREATE TABLE example (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO example (value) VALUES ('ok')")
    connection.commit()
    connection.close()
    output_dir = tmp_path / "outputs" / "worker"
    scratch_root = tmp_path / "scratch-root"
    marker_path = tmp_path / "marker.json"
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    request_path.write_text(
        json.dumps(
            {
                "mutant_id": "m1",
                "db_id": "company",
                "benchmark": "bird",
                "gold_sql": "SELECT 1",
                "mutant_sql": "SELECT 1",
                "source_sqlite_path": str(source_sqlite),
                "scratch_root": str(scratch_root),
                "sqldriller_root": str(sqldriller_root),
                "output_dir": str(output_dir),
            }
        ),
        encoding="utf-8",
    )

    adapter_path = Path(counterexamples.__file__).with_name("adapter.py")
    env = dict(os.environ)
    env["DONATELLO_ADAPTER_MARKER"] = str(marker_path)
    subprocess.run(
        [
            sys.executable,
            str(adapter_path),
            "--request-json",
            str(request_path),
            "--result-json",
            str(result_path),
        ],
        check=True,
        env=env,
    )

    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert Path(marker["schema_db_dir"]).is_relative_to(scratch_root)
    assert Path(marker["db_dir"]).is_relative_to(scratch_root)
    assert marker["sample_is_symlink"] is True
    assert marker["schema_is_symlink"] is True
    assert marker["fuzz_is_symlink"] is True
    assert Path(marker["sample_db"]) == source_sqlite.resolve()
    assert Path(marker["schema_db"]) == source_sqlite.resolve()
    assert Path(marker["fuzz_db"]) == source_sqlite.resolve()


def test_counterexample_adapter_aliases_spider2_to_spider_for_sqldriller_paths(
    tmp_path: Path,
) -> None:
    sqldriller_root = tmp_path / "sqldriller"
    utils_dir = sqldriller_root / "utils"
    utils_dir.mkdir(parents=True)
    (utils_dir / "__init__.py").write_text("", encoding="utf-8")
    (utils_dir / "constants.py").write_text(
        "class _Mode:\n"
        "    exec = 'exec'\n\n"
        "sql_equiv_mode = _Mode()\n",
        encoding="utf-8",
    )
    (utils_dir / "utils.py").write_text(
        "import json\n"
        "import os\n"
        "import sqlite3\n"
        "from pathlib import Path\n\n"
        "def check_equivalence(gold_sql, mutant_sql, _unused, schema_db_dir, db_dir, "
        "db_id, mode, benchmark):\n"
        "    sample_db = Path('data') / benchmark / 'schema_for_sample_values'\n"
        "    sample_db = sample_db / db_id / f'{db_id}.sqlite'\n"
        "    conn = sqlite3.connect(str(sample_db))\n"
        "    conn.execute('select count(*) from sqlite_master').fetchone()\n"
        "    conn.close()\n"
        "    marker = Path(os.environ['DONATELLO_ADAPTER_MARKER'])\n"
        "    marker.write_text(json.dumps({\n"
        "        'benchmark': benchmark,\n"
        "        'sample_db_raw': str(sample_db),\n"
        "        'sample_db_resolved': str(sample_db.resolve()),\n"
        "    }), encoding='utf-8')\n"
        "    return 1, None\n\n"
        "def simplify_ce(*args, **kwargs):\n"
        "    return None\n",
        encoding="utf-8",
    )

    source_sqlite = tmp_path / "source.sqlite"
    connection = sqlite3.connect(source_sqlite)
    connection.execute("CREATE TABLE example (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO example (value) VALUES ('ok')")
    connection.commit()
    connection.close()
    output_dir = tmp_path / "outputs" / "worker"
    scratch_root = tmp_path / "scratch-root"
    marker_path = tmp_path / "marker.json"
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    request_path.write_text(
        json.dumps(
            {
                "mutant_id": "m1",
                "db_id": "RetailOps",
                "benchmark": "spider2",
                "gold_sql": "SELECT 1",
                "mutant_sql": "SELECT 1",
                "source_sqlite_path": str(source_sqlite),
                "scratch_root": str(scratch_root),
                "sqldriller_root": str(sqldriller_root),
                "output_dir": str(output_dir),
            }
        ),
        encoding="utf-8",
    )

    adapter_path = Path(counterexamples.__file__).with_name("adapter.py")
    env = dict(os.environ)
    env["DONATELLO_ADAPTER_MARKER"] = str(marker_path)
    subprocess.run(
        [
            sys.executable,
            str(adapter_path),
            "--request-json",
            str(request_path),
            "--result-json",
            str(result_path),
        ],
        check=True,
        env=env,
    )

    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["benchmark"] == "spider"
    assert marker["sample_db_raw"] == (
        "data/spider/schema_for_sample_values/RetailOps/RetailOps.sqlite"
    )
    assert Path(marker["sample_db_resolved"]) == source_sqlite.resolve()


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
        request = json.loads(request_path.read_text(encoding="utf-8"))
        mutant_id = str(request["mutant_id"])
        output_dir = Path(str(request["output_dir"]))
        seen.append(mutant_id)
        counterexample_path = output_dir / "ce.sqlite"
        counterexample_path.write_text("sqlite", encoding="utf-8")
        result_path.write_text(
            json.dumps({"raw_tag": 0, "counterexample_path": str(counterexample_path)}),
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


def test_counterexample_generation_parallelizes_and_preserves_record_order(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation("first", db_path=db_path),
            _mutation("second", db_path=db_path),
            _mutation("third", db_path=db_path),
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
    write_execution_records(
        artifacts.execution,
        [
            _execution("first", "survived", db_path=db_path),
            _execution("second", "survived", db_path=db_path),
            _execution("third", "survived", db_path=db_path),
        ],
    )

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal active, max_active
        request_path = Path(command[command.index("--request-json") + 1])
        result_path = Path(command[command.index("--result-json") + 1])
        mutant_id = str(
            json.loads(request_path.read_text(encoding="utf-8"))["mutant_id"]
        )
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep({"first": 0.05, "second": 0.01, "third": 0.0}[mutant_id])
        with lock:
            active -= 1
        request = json.loads(request_path.read_text(encoding="utf-8"))
        output_dir = Path(str(request["output_dir"]))
        staged_path = output_dir / f"{mutant_id}.sqlite"
        staged_path.write_text("sqlite", encoding="utf-8")
        payload = {
            "raw_tag": 0,
            "counterexample_path": str(staged_path),
        }
        result_path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    summary = counterexamples.run_counterexample_generation(
        _config(tmp_path, workers=2),
        artifacts=artifacts,
        runner=fake_runner,
    )

    assert summary.selected_count == 3
    assert summary.generated_count == 3
    assert max_active == 2
    rows = counterexamples.read_counterexample_records(artifacts.counterexamples)
    assert [row.mutant_id for row in rows] == ["first", "second", "third"]


def test_counterexample_generation_workers_one_preserves_serial_execution(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation("first", db_path=db_path),
            _mutation("second", db_path=db_path),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            _equivalence("first", "proven_non_equivalent"),
            _equivalence("second", "proven_non_equivalent"),
        ],
    )
    write_execution_records(
        artifacts.execution,
        [
            _execution("first", "survived", db_path=db_path),
            _execution("second", "survived", db_path=db_path),
        ],
    )

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal active, max_active
        result_path = Path(command[command.index("--result-json") + 1])
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        result_path.write_text(
            json.dumps({"raw_tag": 1, "counterexample_path": None}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    summary = counterexamples.run_counterexample_generation(
        _config(tmp_path, workers=1),
        artifacts=artifacts,
        runner=fake_runner,
    )

    assert summary.selected_count == 2
    assert max_active == 1


def test_counterexample_generation_interrupt_does_not_leave_untracked_outputs(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "company.sqlite"
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation("first", db_path=db_path),
            _mutation("second", db_path=db_path),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            _equivalence("first", "proven_non_equivalent"),
            _equivalence("second", "proven_non_equivalent"),
        ],
    )
    write_execution_records(
        artifacts.execution,
        [
            _execution("first", "survived", db_path=db_path),
            _execution("second", "survived", db_path=db_path),
        ],
    )

    def fake_runner(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        request_path = Path(command[command.index("--request-json") + 1])
        result_path = Path(command[command.index("--result-json") + 1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        output_dir = Path(str(request["output_dir"]))
        mutant_id = str(request["mutant_id"])
        if mutant_id == "first":
            time.sleep(0.02)
            raise KeyboardInterrupt
        staged_path = output_dir / f"{mutant_id}.sqlite"
        staged_path.write_text("sqlite", encoding="utf-8")
        result_path.write_text(
            json.dumps({"raw_tag": 0, "counterexample_path": str(staged_path)}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(KeyboardInterrupt):
        counterexamples.run_counterexample_generation(
            _config(tmp_path, workers=2),
            artifacts=artifacts,
            runner=fake_runner,
        )

    assert list(artifacts.counterexamples_dir.iterdir()) == []
    assert counterexamples.read_counterexample_records(artifacts.counterexamples) == []
    assert _staging_dirs(artifacts.root) == []


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
    workers: int = 1,
) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_name="test",
        benchmark="bird",
        bird_mode="dev",
        data_root=tmp_path / "bird",
        output_dir=tmp_path / "outputs",
        workers=workers,
        resume=resume,
        overwrite=overwrite,
        timeouts=TimeoutConfig(sqldriller_seconds=30),
        tools=ToolConfig(
            sqldriller_path=Path("/opt/SQLDriller"),
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


def _staging_dirs(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.iterdir()
        if path.name.startswith("sqldriller-output-")
    )
