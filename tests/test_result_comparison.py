from __future__ import annotations

from typing import cast

import pytest

from donatello.result_comparison import ResultSemantics, results_equal


def test_bag_semantics_preserve_duplicates_but_ignore_order() -> None:
    assert results_equal(
        [(1,), (2,), (1,)],
        [(1,), (1,), (2,)],
        semantics="bag",
    )
    assert not results_equal(
        [(1,), (1,), (2,)],
        [(1,), (2,)],
        semantics="bag",
    )


def test_set_semantics_ignore_duplicates_and_order() -> None:
    assert results_equal(
        [(1,), (1,), (2,)],
        [(2,), (1,)],
        semantics="set",
    )


def test_unknown_result_semantics_are_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported result semantics"):
        results_equal(
            [(1,)],
            [(1,)],
            semantics=cast(ResultSemantics, "sequence"),
        )
