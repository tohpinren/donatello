"""Benchmark adapter dispatch."""

from __future__ import annotations

from sqlmt.benchmarks.base import BenchmarkError
from sqlmt.benchmarks.bird import BirdAdapter
from sqlmt.config import ExperimentConfig
from sqlmt.models import NormalizedExample


def normalize_examples(config: ExperimentConfig) -> list[NormalizedExample]:
    """Normalize the configured benchmark into the shared row contract."""

    if config.benchmark == "bird":
        return BirdAdapter().normalize(config)

    raise BenchmarkError(
        f"Normalization for benchmark '{config.benchmark}' is not implemented yet."
    )


__all__ = ["BenchmarkError", "normalize_examples"]
