"""Explicit row-collection semantics for execution-result comparison."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any, Literal

ResultSemantics = Literal["bag", "set"]


def results_equal(
    left: Iterable[tuple[Any, ...]],
    right: Iterable[tuple[Any, ...]],
    *,
    semantics: ResultSemantics,
) -> bool:
    """Compare query results using order-insensitive bag or set semantics.

    Bag semantics preserve duplicate multiplicities. Set semantics discard
    duplicates. Neither mode treats row order as significant.
    """

    if semantics == "bag":
        return Counter(left) == Counter(right)
    if semantics == "set":
        return set(left) == set(right)
    raise ValueError(f"Unsupported result semantics: {semantics}")
