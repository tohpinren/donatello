"""Experiment artifact path conventions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExperimentArtifacts:
    """Resolved output paths for one experiment run."""

    root: Path

    @property
    def config_snapshot(self) -> Path:
        """Return the copied config file stored alongside a run."""

        return self.root / "config.yaml"

    @property
    def run_log(self) -> Path:
        """Return the path of the run-local log file."""

        return self.root / "run.log"

    @property
    def normalized_examples(self) -> Path:
        """Return the path of the normalized benchmark artifact."""

        return self.root / "normalized_examples.jsonl"

    @property
    def mutants(self) -> Path:
        """Return the path of the mutation-generation artifact."""

        return self.root / "mutants.jsonl"

    @property
    def equivalence(self) -> Path:
        """Return the reserved path for equivalence-filtering results."""

        return self.root / "equivalence.jsonl"

    @property
    def execution(self) -> Path:
        """Return the reserved path for SQLite execution results."""

        return self.root / "execution.jsonl"

    @property
    def counterexamples_dir(self) -> Path:
        """Return the directory reserved for generated counterexamples."""

        return self.root / "counterexamples"

    @property
    def augmented_databases_dir(self) -> Path:
        """Return the directory reserved for augmented database copies."""

        return self.root / "augmented_databases"

    @property
    def report(self) -> Path:
        """Return the reserved path for the top-level report artifact."""

        return self.root / "report.md"

    def ensure_base_dirs(self) -> None:
        """Create the output directories needed before a run can write logs."""

        self.root.mkdir(parents=True, exist_ok=True)

    def as_display_dict(self) -> dict[str, str]:
        """Return stable path names for dry-run and test output."""

        return {
            "root": str(self.root),
            "config_snapshot": str(self.config_snapshot),
            "run_log": str(self.run_log),
            "normalized_examples": str(self.normalized_examples),
            "mutants": str(self.mutants),
            "equivalence": str(self.equivalence),
            "execution": str(self.execution),
            "counterexamples_dir": str(self.counterexamples_dir),
            "augmented_databases_dir": str(self.augmented_databases_dir),
            "report": str(self.report),
        }


def artifacts_for(output_dir: Path) -> ExperimentArtifacts:
    """Build artifact paths for an already-resolved output directory."""

    return ExperimentArtifacts(root=output_dir)
