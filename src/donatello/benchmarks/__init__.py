"""Benchmark adapter dispatch."""

from __future__ import annotations

from donatello.benchmarks.base import BenchmarkError
from donatello.benchmarks.bird import BirdAdapter
from donatello.benchmarks.spider2 import Spider2Adapter
from donatello.config import ExperimentConfig
from donatello.models import NormalizedExample


def normalize_examples(config: ExperimentConfig) -> list[NormalizedExample]:
    """Normalize the configured benchmark into the shared row contract."""

    if config.benchmark == "bird":
        return BirdAdapter().normalize(config)
    if config.benchmark == "spider2":
        return Spider2Adapter().normalize(config)

    raise BenchmarkError(
        f"Normalization for benchmark '{config.benchmark}' is not implemented yet."
    )


__all__ = ["BenchmarkError", "normalize_examples"]
