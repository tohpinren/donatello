"""Canonical mutation profile and small rule contracts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlglot import exp

from sqlmt.models import NormalizedExample

RuleApply = Callable[[NormalizedExample, exp.Expression], list["MutationCandidate"]]

CANONICAL_RULE_IDS = (
    "a_null_pred",
    "a_topk_ties",
    "a_agg_edge",
    "a_pred_loc",
    "a_subq_corr_drop",
    "b_join_miss",
    "b_from_unused_tv",
    "b_subq_scope_typo",
    "b_like_nowildcard",
    "b_wildcard_wrong_op",
    "b_domain_mismatch_comp",
    "c_pred_ror",
    "c_pred_lcr",
    "c_multi_dist",
    "c_multi_setop",
    "c_group_agg",
    "c_group_key",
    "c_bin_join",
    "c_sub_qual",
)


@dataclass(frozen=True)
class MutationCandidate:
    """One raw mutant candidate emitted by a mutation rule."""

    mutant_sql: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MutationRule:
    """A named mutation rule callable used by the generator."""

    rule_id: str
    apply: RuleApply


def canonical_rules() -> list[MutationRule]:
    """Return the canonical 19-rule mutation profile in fixed order."""

    from sqlmt.mutation.rules_a import RULES_A
    from sqlmt.mutation.rules_b import RULES_B
    from sqlmt.mutation.rules_c import RULES_C

    rules = [*RULES_A, *RULES_B, *RULES_C]
    if [rule.rule_id for rule in rules] != list(CANONICAL_RULE_IDS):
        raise RuntimeError("Canonical mutation rule registry is out of sync.")
    return rules


def ensure_rule_sequence(rules: Sequence[MutationRule] | None) -> list[MutationRule]:
    """Normalize an optional rule sequence into a concrete list."""

    return canonical_rules() if rules is None else list(rules)
