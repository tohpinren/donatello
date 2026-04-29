from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from sqlmt.artifacts import artifacts_for
from sqlmt.augmentation import write_augmentation_records
from sqlmt.counterexamples import write_counterexample_records
from sqlmt.execution import write_execution_records
from sqlmt.models import (
    AugmentationRecord,
    CounterexampleRecord,
    ExecutionOutcome,
    ExecutionRecord,
)
from sqlmt.reporting import run_reporting


def test_reporting_writes_markdown_and_json_summary(tmp_path: Path) -> None:
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()
    write_execution_records(
        artifacts.execution,
        [
            _execution("killed", "killed", "c_multi_dist"),
            _execution("survived", "survived", "c_multi_dist"),
        ],
    )
    write_counterexample_records(
        artifacts.counterexamples,
        [_counterexample("survived")],
    )
    artifacts.augmented_databases_dir.mkdir(parents=True)
    write_augmentation_records(
        artifacts.augmentation,
        [
            AugmentationRecord(
                source_sqlite_path="/tmp/company.sqlite",
                augmented_sqlite_path="/tmp/augmented/company.sqlite",
                db_id="company",
                mutant_ids=["survived"],
                counterexample_count=1,
                inserted_row_count=1,
                skipped_row_count=0,
                warnings=[],
                errors=[],
            )
        ],
    )
    write_execution_records(
        artifacts.augmented_execution,
        [
            _execution("survived", "killed", "c_multi_dist"),
        ],
    )

    summary = run_reporting(_config(tmp_path), artifacts=artifacts)

    assert summary.output_path == str(artifacts.report)
    assert summary.json_output_path == str(artifacts.report_json)
    report_text = artifacts.report.read_text(encoding="utf-8")
    assert "# SQL Mutation Testing Report" in report_text
    assert "Original mutation score: 0.5000" in report_text
    assert "Augmented mutation score: 1.0000" in report_text
    assert "Score delta: 0.5000" in report_text
    payload = json.loads(artifacts.report_json.read_text(encoding="utf-8"))
    assert payload["scores"]["original"] == 0.5
    assert payload["scores"]["augmented"] == 1.0
    assert payload["scores"]["delta"] == 0.5
    assert payload["per_rule"]["c_multi_dist"]["original"]["survived"] == 1
    assert payload["per_rule"]["c_multi_dist"]["augmented"]["killed"] == 1


def test_reporting_marks_missing_sections_unavailable(tmp_path: Path) -> None:
    artifacts = artifacts_for(tmp_path / "outputs")
    artifacts.ensure_base_dirs()

    summary = run_reporting(_config(tmp_path), artifacts=artifacts)

    assert summary.sections["execution"] == "unavailable"
    assert summary.sections["counterexamples"] == "unavailable"
    payload = json.loads(artifacts.report_json.read_text(encoding="utf-8"))
    assert payload["sections"]["execution"] == "unavailable"
    assert "unavailable" in artifacts.report.read_text(encoding="utf-8")


def _execution(mutant_id: str, outcome: str, rule: str) -> ExecutionRecord:
    return ExecutionRecord(
        mutant_id=mutant_id,
        example_id=f"bird:dev:{mutant_id}",
        outcome=cast(ExecutionOutcome, outcome),
        gold_sql="SELECT name FROM employees ORDER BY id",
        mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
        sqlite_path="/tmp/company.sqlite",
        rule=rule,
        profile="canonical",
    )


def _counterexample(mutant_id: str) -> CounterexampleRecord:
    return CounterexampleRecord(
        mutant_id=mutant_id,
        example_id=f"bird:dev:{mutant_id}",
        outcome="generated",
        gold_sql="SELECT name FROM employees ORDER BY id",
        mutant_sql="SELECT name FROM employees WHERE id = 1 ORDER BY id",
        source_sqlite_path="/tmp/company.sqlite",
        rule="c_multi_dist",
        profile="canonical",
        counterexample_path="/tmp/ce.sqlite",
        raw_tag=0,
    )


def _config(tmp_path: Path):
    from sqlmt.config import ExperimentConfig, StageConfig

    return ExperimentConfig(
        experiment_name="test",
        benchmark="bird",
        bird_mode="dev",
        data_root=tmp_path / "bird",
        output_dir=tmp_path / "outputs",
        stages=StageConfig(
            normalize=False,
            mutate=False,
            equivalence=False,
            execute=False,
            counterexamples=False,
            augment=False,
            report=True,
        ),
    )
