"""Minimal benchmark normalization adapter contracts."""

from __future__ import annotations

from typing import Protocol

from sqlmt.config import ExperimentConfig
from sqlmt.models import NormalizedExample


class BenchmarkError(RuntimeError):
    """Raised when benchmark normalization cannot proceed."""


class BenchmarkAdapter(Protocol):
    """Small shared interface for benchmark-specific normalization."""

    def normalize(self, config: ExperimentConfig) -> list[NormalizedExample]:
        """Normalize a benchmark into the shared example contract."""
