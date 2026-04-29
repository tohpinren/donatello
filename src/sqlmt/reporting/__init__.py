"""Markdown and JSON reporting for experiment artifacts."""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from typing import Any

from sqlmt.artifacts import ExperimentArtifacts
from sqlmt.augmentation import read_augmentation_records
from sqlmt.config import ExperimentConfig
from sqlmt.counterexamples import read_counterexample_records, summarize_counterexamples
from sqlmt.execution import read_execution_records, summarize_execution
from sqlmt.models import ExecutionRecord, ReportSummary

LOGGER = logging.getLogger(__name__)


def run_reporting(
    config: ExperimentConfig,
    *,
    artifacts: ExperimentArtifacts,
) -> ReportSummary:
    """Write Markdown and JSON summaries for available pipeline artifacts."""

    LOGGER.info(
        "Reporting stage start output=%s json_output=%s",
        artifacts.report,
        artifacts.report_json,
    )
    existing_outputs = [
        path for path in (artifacts.report, artifacts.report_json) if path.exists()
    ]
    if existing_outputs:
        if config.resume:
            LOGGER.info(
                "Reporting stage resume outputs=%s",
                ",".join(path.name for path in existing_outputs),
            )
            return _summarize_existing_report(artifacts)
        if not config.overwrite:
            raise ValueError(
                "Report outputs already exist: "
                + ", ".join(path.name for path in existing_outputs)
                + ". Set resume: true to reuse them or overwrite: true to replace "
                "them."
            )
    if config.overwrite:
        for path in existing_outputs:
            LOGGER.info("Reporting overwrite removing output=%s", path)
            path.unlink()

    payload = _build_payload(config, artifacts)
    artifacts.report.write_text(_render_markdown(payload), encoding="utf-8")
    artifacts.report_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    LOGGER.info(
        "Reporting stage complete output=%s json_output=%s sections=%s "
        "original_score=%s augmented_score=%s",
        artifacts.report,
        artifacts.report_json,
        ",".join(f"{name}:{status}" for name, status in payload["sections"].items()),
        payload["scores"]["original"],
        payload["scores"]["augmented"],
    )
    return ReportSummary(
        output_path=str(artifacts.report),
        json_output_path=str(artifacts.report_json),
        sections=dict(payload["sections"]),
        skipped_stages=[],
    )


def _summarize_existing_report(artifacts: ExperimentArtifacts) -> ReportSummary:
    sections: dict[str, str] = {}
    if artifacts.report_json.exists():
        try:
            payload = json.loads(artifacts.report_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("sections"), dict):
            sections = {
                str(name): str(status) for name, status in payload["sections"].items()
            }
    return ReportSummary(
        output_path=str(artifacts.report),
        json_output_path=str(artifacts.report_json),
        sections=sections,
        resumed=True,
        skipped_stages=[],
    )


def _build_payload(
    config: ExperimentConfig,
    artifacts: ExperimentArtifacts,
) -> dict[str, Any]:
    sections = {
        "execution": "available" if artifacts.execution.exists() else "unavailable",
        "counterexamples": (
            "available" if artifacts.counterexamples.exists() else "unavailable"
        ),
        "augmentation": (
            "available" if artifacts.augmentation.exists() else "unavailable"
        ),
        "augmented_execution": (
            "available" if artifacts.augmented_execution.exists() else "unavailable"
        ),
    }
    LOGGER.info(
        "Reporting artifact availability execution=%s counterexamples=%s "
        "augmentation=%s augmented_execution=%s",
        sections["execution"],
        sections["counterexamples"],
        sections["augmentation"],
        sections["augmented_execution"],
    )
    execution_records = (
        read_execution_records(artifacts.execution)
        if artifacts.execution.exists()
        else []
    )
    augmented_execution_records = (
        read_execution_records(artifacts.augmented_execution)
        if artifacts.augmented_execution.exists()
        else []
    )
    execution_summary = (
        summarize_execution(execution_records, output_path=artifacts.execution)
        if execution_records
        else None
    )
    augmented_execution_summary = (
        summarize_execution(
            augmented_execution_records,
            output_path=artifacts.augmented_execution,
        )
        if augmented_execution_records
        else None
    )
    counterexample_records = (
        read_counterexample_records(artifacts.counterexamples)
        if artifacts.counterexamples.exists()
        else []
    )
    counterexample_summary = (
        summarize_counterexamples(
            counterexample_records,
            output_path=artifacts.counterexamples,
            output_dir=artifacts.counterexamples_dir,
        )
        if counterexample_records
        else None
    )
    augmentation_records = (
        read_augmentation_records(artifacts.augmentation)
        if artifacts.augmentation.exists()
        else []
    )
    original_score = (
        execution_summary.mutation_score if execution_summary is not None else None
    )
    augmented_score = (
        augmented_execution_summary.mutation_score
        if augmented_execution_summary is not None
        else None
    )
    return {
        "experiment": {
            "name": config.experiment_name,
            "benchmark": config.benchmark,
            "mode": config.bird_mode,
            "enabled_stages": config.stages.enabled(),
        },
        "artifacts": artifacts.as_display_dict(),
        "sections": sections,
        "scores": {
            "original": original_score,
            "augmented": augmented_score,
            "delta": (
                augmented_score - original_score
                if original_score is not None and augmented_score is not None
                else None
            ),
        },
        "execution": (
            execution_summary.__dict__ if execution_summary is not None else None
        ),
        "counterexamples": (
            counterexample_summary.__dict__
            if counterexample_summary is not None
            else None
        ),
        "augmentation": _augmentation_payload(augmentation_records),
        "augmented_execution": (
            augmented_execution_summary.__dict__
            if augmented_execution_summary is not None
            else None
        ),
        "per_rule": _per_rule(execution_records, augmented_execution_records),
        "warnings": [
            warning for record in augmentation_records for warning in record.warnings
        ],
    }


def _augmentation_payload(records) -> dict[str, Any] | None:
    if not records:
        return None
    return {
        "database_count": len(records),
        "counterexample_count": sum(record.counterexample_count for record in records),
        "inserted_row_count": sum(record.inserted_row_count for record in records),
        "skipped_row_count": sum(record.skipped_row_count for record in records),
        "warning_count": sum(len(record.warnings) for record in records),
        "error_count": sum(len(record.errors) for record in records),
    }


def _per_rule(
    execution_records: list[ExecutionRecord],
    augmented_execution_records: list[ExecutionRecord],
) -> dict[str, dict[str, dict[str, int]]]:
    rules = sorted(
        {record.rule for record in execution_records}
        | {record.rule for record in augmented_execution_records}
    )
    original = _rule_counts(execution_records)
    augmented = _rule_counts(augmented_execution_records)
    return {
        rule: {
            "original": dict(original.get(rule, Counter())),
            "augmented": dict(augmented.get(rule, Counter())),
        }
        for rule in rules
    }


def _rule_counts(records: list[ExecutionRecord]) -> dict[str, Counter[str]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        counts[record.rule][record.outcome] += 1
    return counts


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# SQL Mutation Testing Report",
        "",
        f"Experiment: {payload['experiment']['name']}",
        f"Benchmark: {payload['experiment']['benchmark']}",
        f"Mode: {payload['experiment']['mode']}",
        "",
        "## Scores",
        "",
        f"Original mutation score: {_format_score(payload['scores']['original'])}",
        f"Augmented mutation score: {_format_score(payload['scores']['augmented'])}",
        f"Score delta: {_format_score(payload['scores']['delta'])}",
        "",
        "## Sections",
        "",
    ]
    for name, status in payload["sections"].items():
        lines.append(f"- {name}: {status}")
    lines.extend(["", "## Counts", ""])
    if payload["counterexamples"] is not None:
        counterexamples = payload["counterexamples"]
        lines.append(
            "Counterexamples: "
            f"{counterexamples['generated_count']} generated, "
            f"{counterexamples['not_generated_count']} not generated, "
            f"{counterexamples['timeout_count']} timeout, "
            f"{counterexamples['error_count']} error"
        )
    else:
        lines.append("Counterexamples: unavailable")
    if payload["augmentation"] is not None:
        augmentation = payload["augmentation"]
        lines.append(
            "Augmentation: "
            f"{augmentation['database_count']} databases, "
            f"{augmentation['counterexample_count']} counterexamples, "
            f"{augmentation['inserted_row_count']} inserted rows, "
            f"{augmentation['skipped_row_count']} skipped rows"
        )
    else:
        lines.append("Augmentation: unavailable")
    lines.extend(["", "## Per Rule", ""])
    if payload["per_rule"]:
        lines.append(
            "| Rule | Original killed | Original survived | "
            "Augmented killed | Augmented survived |"
        )
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for rule, counts in payload["per_rule"].items():
            original = counts["original"]
            augmented = counts["augmented"]
            lines.append(
                f"| {rule} | {original.get('killed', 0)} | "
                f"{original.get('survived', 0)} | "
                f"{augmented.get('killed', 0)} | "
                f"{augmented.get('survived', 0)} |"
            )
    else:
        lines.append("No per-rule execution results available.")
    lines.extend(["", "## Warnings", ""])
    if payload["warnings"]:
        lines.extend(f"- {warning}" for warning in payload["warnings"])
    else:
        lines.append("No augmentation warnings.")
    lines.append("")
    return "\n".join(lines)


def _format_score(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "unavailable"
