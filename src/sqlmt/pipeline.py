"""Pipeline orchestration placeholders for the foundation phase."""

from __future__ import annotations

from dataclasses import dataclass

from sqlmt.artifacts import ExperimentArtifacts, artifacts_for
from sqlmt.config import ExperimentConfig


@dataclass(frozen=True)
class DryRunPlan:
    experiment_name: str
    output_dir: str
    enabled_stages: list[str]
    artifacts: dict[str, str]


def build_dry_run_plan(config: ExperimentConfig) -> DryRunPlan:
    artifacts = artifacts_for(config.output_dir)
    return DryRunPlan(
        experiment_name=config.experiment_name,
        output_dir=str(artifacts.root),
        enabled_stages=config.stages.enabled(),
        artifacts=artifacts.as_display_dict(),
    )


def prepare_run(config: ExperimentConfig) -> ExperimentArtifacts:
    artifacts = artifacts_for(config.output_dir)
    artifacts.ensure_base_dirs()
    return artifacts
