from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from donatello.artifacts import artifacts_for
from donatello.config import ExperimentConfig, StageConfig, TimeoutConfig, ToolConfig
from donatello.equivalence import (
    build_verieql_input_rows,
    read_equivalence_records,
    run_equivalence_filtering,
    write_equivalence_records,
)
from donatello.models import EquivalenceRecord, MutationRecord, MutationStatus
from donatello.mutation.generator import write_mutation_records
from tests.helpers import (
    make_normalized_example,
    read_jsonl,
    write_normalized_examples,
)


def test_verieql_input_uses_generated_mutants_and_schema() -> None:
    example = make_normalized_example(
        "SELECT name FROM employees WHERE salary >= 100",
    )
    generated = _mutation(
        mutant_id="m1",
        gold_sql=example.gold_sql,
        mutant_sql="SELECT name FROM employees WHERE salary > 100",
    )
    skipped = _mutation(mutant_id="m2", status="skipped", mutant_sql=None)

    rows = build_verieql_input_rows([generated, skipped], [example])

    assert len(rows) == 1
    row = rows[0]
    assert row["index"] == 0
    assert row["file"] == "m1"
    assert row["pair"] == [
        "SELECT name FROM employees WHERE salary >= 100",
        "SELECT name FROM employees WHERE salary > 100",
    ]
    schema = cast(dict[str, dict[str, str]], row["schema"])
    constraints = cast(list[Any], row["constraint"])
    assert schema["EMPLOYEES"]["SALARY"] == "INT"
    assert schema["DEPARTMENTS"]["ID"] == "INT"
    assert {"primary": [{"value": "EMPLOYEES__ID"}]} in constraints


def test_run_equivalence_filtering_maps_verieql_states_and_writes_artifact(
    tmp_path: Path,
) -> None:
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    example = make_normalized_example(
        "SELECT name FROM employees WHERE salary >= 100",
    )
    write_normalized_examples(artifacts.normalized_examples, [example])
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation(
                mutant_id="equ",
                gold_sql=example.gold_sql,
                mutant_sql="SELECT DISTINCT name FROM employees WHERE salary >= 100",
            ),
            _mutation(
                mutant_id="neq",
                gold_sql=example.gold_sql,
                mutant_sql="SELECT name FROM employees WHERE salary > 100",
            ),
            _mutation(
                mutant_id="timeout",
                gold_sql=example.gold_sql,
                mutant_sql="SELECT name FROM employees WHERE salary < 100",
            ),
            _mutation(
                mutant_id="unsupported",
                gold_sql=example.gold_sql,
                mutant_sql="SELECT name FROM employees WHERE salary <> 100",
            ),
            _mutation(
                mutant_id="not_implemented",
                gold_sql=example.gold_sql,
                mutant_sql="SELECT name FROM employees WHERE name LIKE 'A%'",
            ),
            _mutation(
                mutant_id="error",
                gold_sql=example.gold_sql,
                mutant_sql="SELECT name FROM employees WHERE salary = 100",
            ),
            _mutation(mutant_id="skipped", status="skipped", mutant_sql=None),
        ],
    )
    config = _config(tmp_path)

    summary = run_equivalence_filtering(
        config,
        artifacts=artifacts,
        runner=_fake_verieql_runner,
    )

    assert summary.checked_count == 6
    assert summary.proven_equivalent_count == 1
    assert summary.proven_non_equivalent_count == 1
    assert summary.timeout_count == 1
    assert summary.unsupported_count == 2
    assert summary.error_count == 1
    assert summary.output_path == str(artifacts.equivalence)

    rows = read_jsonl(artifacts.equivalence)
    assert [row["mutant_id"] for row in rows] == [
        "equ",
        "neq",
        "timeout",
        "unsupported",
        "not_implemented",
        "error",
    ]
    assert [row["outcome"] for row in rows] == [
        "proven_equivalent",
        "proven_non_equivalent",
        "timeout",
        "unsupported",
        "unsupported",
        "error",
    ]
    assert rows[4]["raw_state"] == "NIE"
    assert rows[5]["raw_state"] == "SYN"
    assert rows[5]["error"] == "syntax"


def test_run_equivalence_filtering_resumes_existing_artifact(
    tmp_path: Path,
) -> None:
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    existing = [
        EquivalenceRecord(
            mutant_id="m1",
            example_id="bird:dev:000000",
            outcome="proven_non_equivalent",
            raw_state="NEQ",
            timeout_seconds=120,
        )
    ]
    write_equivalence_records(artifacts.equivalence, existing)

    summary = run_equivalence_filtering(
        _config(tmp_path),
        artifacts=artifacts,
        runner=_runner_that_should_not_run,
    )

    assert summary.resumed is True
    assert summary.checked_count == 1
    assert read_equivalence_records(artifacts.equivalence) == existing


def test_run_equivalence_filtering_resumes_partial_artifact_and_runs_missing(
    tmp_path: Path,
) -> None:
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    example = make_normalized_example(
        "SELECT name FROM employees WHERE salary >= 100",
    )
    write_normalized_examples(artifacts.normalized_examples, [example])
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation(
                mutant_id="done",
                gold_sql=example.gold_sql,
                mutant_sql="SELECT DISTINCT name FROM employees WHERE salary >= 100",
            ),
            _mutation(
                mutant_id="pending",
                gold_sql=example.gold_sql,
                mutant_sql="SELECT name FROM employees WHERE salary > 100",
            ),
        ],
    )
    write_equivalence_records(
        artifacts.equivalence,
        [
            EquivalenceRecord(
                mutant_id="done",
                example_id="bird:dev:000000",
                outcome="proven_equivalent",
                raw_state="EQU",
                timeout_seconds=120,
            )
        ],
    )

    summary = run_equivalence_filtering(
        _config(tmp_path),
        artifacts=artifacts,
        runner=_resume_missing_runner,
    )

    assert summary.resumed is True
    assert summary.checked_count == 2
    assert summary.proven_equivalent_count == 1
    assert summary.proven_non_equivalent_count == 1
    assert [
        record.mutant_id
        for record in read_equivalence_records(artifacts.equivalence)
    ] == ["done", "pending"]


def test_run_equivalence_filtering_salvages_partial_output_on_timeout(
    tmp_path: Path,
) -> None:
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    example = make_normalized_example(
        "SELECT name FROM employees WHERE salary >= 100",
    )
    write_normalized_examples(artifacts.normalized_examples, [example])
    write_mutation_records(
        artifacts.mutants,
        [
            _mutation(
                mutant_id="done",
                gold_sql=example.gold_sql,
                mutant_sql="SELECT DISTINCT name FROM employees WHERE salary >= 100",
            ),
            _mutation(
                mutant_id="pending",
                gold_sql=example.gold_sql,
                mutant_sql="SELECT name FROM employees WHERE salary > 100",
            ),
        ],
    )

    summary = run_equivalence_filtering(
        _config(tmp_path),
        artifacts=artifacts,
        runner=_timeout_after_partial_output_runner,
    )

    assert summary.checked_count == 2
    assert summary.proven_equivalent_count == 1
    assert summary.timeout_count == 1
    rows = read_jsonl(artifacts.equivalence)
    assert rows[0] == {
        "mutant_id": "done",
        "example_id": "bird:dev:000000",
        "outcome": "proven_equivalent",
        "raw_state": "EQU",
        "timeout_seconds": 125,
        "verieql_index": 0,
        "times": [[0.1, 0.2]],
        "counterexample_sql": None,
        "error": None,
        "metadata": {},
    }
    assert rows[1]["mutant_id"] == "pending"
    assert rows[1]["outcome"] == "timeout"
    assert rows[1]["raw_state"] == "TMO"
    assert rows[1]["timeout_seconds"] == 125
    assert "timed out" in rows[1]["error"]


def test_run_equivalence_filtering_requires_tool_config(tmp_path: Path) -> None:
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()

    with pytest.raises(ValueError, match="tools.verieql_path"):
        run_equivalence_filtering(
            _config(tmp_path, verieql_path=None),
            artifacts=artifacts,
            runner=_runner_that_should_not_run,
        )

    with pytest.raises(ValueError, match="tools.verieql_python"):
        run_equivalence_filtering(
            _config(tmp_path, verieql_python=None),
            artifacts=artifacts,
            runner=_runner_that_should_not_run,
        )


def _fake_verieql_runner(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    assert cwd.name == "VeriEQL"
    assert timeout == 810
    assert command[:3] == [
        "/tmp/verieql/bin/python",
        "-m",
        "parallel.cli_within_timeout",
    ]
    input_path = Path(command[command.index("-f") + 1])
    output_path = Path(command[command.index("-o") + 1])
    input_rows = read_jsonl(input_path)
    assert [row["file"] for row in input_rows] == [
        "equ",
        "neq",
        "timeout",
        "unsupported",
        "not_implemented",
        "error",
    ]

    states_by_mutant = {
        "equ": ("EQU", None),
        "neq": ("NEQ", "counterexample"),
        "timeout": ("TMO", None),
        "unsupported": ("NSE", None),
        "not_implemented": ("NIE", None),
        "error": ("SYN", None),
    }
    with output_path.open("w", encoding="utf-8") as file:
        for row in input_rows:
            state, counterexample = states_by_mutant[str(row["file"])]
            error = "syntax" if state == "SYN" else None
            file.write(
                json.dumps(
                    {
                        "index": row["index"],
                        "file": row["file"],
                        "states": [state],
                        "times": [[0.1, 0.2]],
                        "counterexample": counterexample,
                        "err": error,
                    }
                )
                + "\n"
            )
    return subprocess.CompletedProcess(command, 0, "", "")


def _runner_that_should_not_run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    raise AssertionError("runner should not be called")


def _resume_missing_runner(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    input_path = Path(command[command.index("-f") + 1])
    output_path = Path(command[command.index("-o") + 1])
    rows = read_jsonl(input_path)
    assert [row["file"] for row in rows] == ["pending"]
    output_path.write_text(
        json.dumps(
            {
                "index": 0,
                "file": "pending",
                "states": ["NEQ"],
                "times": [[0.1, 0.2]],
                "counterexample": "ce",
                "err": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return subprocess.CompletedProcess(command, 0, "", "")


def _timeout_after_partial_output_runner(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    input_path = Path(command[command.index("-f") + 1])
    output_path = Path(command[command.index("-o") + 1])
    rows = read_jsonl(input_path)
    assert [row["file"] for row in rows] == ["done", "pending"]
    shard_path = output_path.parent / f"{output_path.name}0"
    shard_path.write_text(
        json.dumps(
            {
                "index": 0,
                "file": "done",
                "states": ["EQU"],
                "times": [[0.1, 0.2]],
                "counterexample": None,
                "err": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)


def _config(
    tmp_path: Path,
    *,
    verieql_path: Path | None = Path("/tmp/VeriEQL"),
    verieql_python: Path | None = Path("/tmp/verieql/bin/python"),
) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_name="test",
        benchmark="bird",
        bird_mode="dev",
        data_root=tmp_path / "bird",
        output_dir=tmp_path / "outputs",
        workers=2,
        stages=StageConfig(equivalence=True),
        timeouts=TimeoutConfig(verieql_seconds=125),
        tools=ToolConfig(
            verieql_path=verieql_path,
            verieql_python=verieql_python,
        ),
    )


def _mutation(
    *,
    mutant_id: str,
    gold_sql: str = "SELECT name FROM employees",
    mutant_sql: str | None = "SELECT DISTINCT name FROM employees",
    status: MutationStatus = "generated",
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
        status=status,
        skip_reason="rule_not_applicable" if status == "skipped" else None,
        gold_sql=gold_sql,
        mutant_sql=mutant_sql,
        sqlite_path="/tmp/company.sqlite",
        sqlite_relpath="dev/dev_databases/company/company.sqlite",
    )
