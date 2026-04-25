"""Canonical set C mutation rules."""

from __future__ import annotations

from typing import cast

from sqlglot import exp

from sqlmt.models import NormalizedExample
from sqlmt.mutation.helpers import (
    clone_expression,
    comparison_nodes,
    contains_aggregate,
    first_visible_column,
    group_key_sqls,
    join_conditions,
    local_alias_map,
    matching_nodes,
    nth_match,
    select_projection_columns,
    serialize_sql,
    split_and_conditions,
    top_level_select,
    visible_columns,
)
from sqlmt.mutation.profile import MutationCandidate, MutationRule

RELATIONAL_REPLACEMENTS: dict[type[exp.Expression], list[type[exp.Expression]]] = {
    exp.EQ: [exp.NEQ, exp.GT, exp.LT],
    exp.NEQ: [exp.EQ, exp.GT, exp.LT],
    exp.GT: [exp.GTE, exp.LT, exp.LTE],
    exp.GTE: [exp.GT, exp.LT, exp.LTE],
    exp.LT: [exp.LTE, exp.GT, exp.GTE],
    exp.LTE: [exp.LT, exp.GT, exp.GTE],
}

AGGREGATE_ALTERNATIVES: dict[type[exp.Expression], list[type[exp.Expression]]] = {
    exp.Count: [exp.Sum, exp.Avg, exp.Min, exp.Max],
    exp.Sum: [exp.Count, exp.Avg, exp.Min, exp.Max],
    exp.Avg: [exp.Count, exp.Sum, exp.Min, exp.Max],
    exp.Min: [exp.Count, exp.Sum, exp.Avg, exp.Max],
    exp.Max: [exp.Count, exp.Sum, exp.Avg, exp.Min],
}


def c_pred_ror(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Replace comparison operators with deterministic relational alternatives."""

    candidates: list[MutationCandidate] = []
    for site_index, comparison in enumerate(comparison_nodes(tree)):
        replacement_types = RELATIONAL_REPLACEMENTS.get(type(comparison), [])
        for replacement_index, replacement_type in enumerate(replacement_types[:3]):
            mutated = clone_expression(tree)
            target = list(comparison_nodes(mutated))[site_index]
            replacement = replacement_type(
                this=target.this.copy(),
                expression=target.expression.copy(),
            )
            target.replace(replacement)
            candidates.append(
                MutationCandidate(
                    mutant_sql=serialize_sql(mutated),
                    metadata={
                        "site_index": site_index,
                        "from": comparison.key.upper(),
                        "to": replacement.key.upper(),
                        "replacement_index": replacement_index,
                    },
                )
            )
    return candidates


def c_pred_lcr(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Swap top-level logical connectors between ``AND`` and ``OR``."""

    candidates: list[MutationCandidate] = []

    for site_index, logical in enumerate(
        matching_nodes(tree, exp.And) + matching_nodes(tree, exp.Or)
    ):
        if not isinstance(logical, (exp.And, exp.Or)):
            continue
        mutated = clone_expression(tree)
        matches = matching_nodes(mutated, exp.And) + matching_nodes(mutated, exp.Or)
        target = matches[site_index]
        replacement = (
            exp.Or(this=target.this.copy(), expression=target.expression.copy())
            if isinstance(target, exp.And)
            else exp.And(this=target.this.copy(), expression=target.expression.copy())
        )
        target.replace(replacement)
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={
                    "site_index": site_index,
                    "from": logical.key.upper(),
                    "to": replacement.key.upper(),
                },
            )
        )

    return candidates


def c_multi_dist(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Toggle top-level ``SELECT DISTINCT`` semantics."""

    select = top_level_select(tree)
    if select is None:
        return []

    mutated = clone_expression(select)
    current_distinct = mutated.args.get("distinct")
    if current_distinct is None:
        mutated.set("distinct", exp.Distinct())
        action = ("NONE", "DISTINCT")
    else:
        mutated.set("distinct", None)
        action = ("DISTINCT", "NONE")
    return [
        MutationCandidate(
            mutant_sql=serialize_sql(mutated),
            metadata={"site_index": 0, "from": action[0], "to": action[1]},
        )
    ]


def c_multi_setop(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Swap ``UNION`` and ``UNION ALL`` in set-operation queries."""

    candidates: list[MutationCandidate] = []
    for site_index, _union in enumerate(matching_nodes(tree, exp.Union)):
        mutated = clone_expression(tree)
        target = nth_match(mutated, exp.Union, site_index)
        if not isinstance(target, exp.Union):
            continue
        current_distinct = bool(target.args.get("distinct", True))
        target.set("distinct", not current_distinct)
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={
                    "site_index": site_index,
                    "from": "UNION" if current_distinct else "UNION ALL",
                    "to": "UNION ALL" if current_distinct else "UNION",
                },
            )
        )
    return candidates


def c_group_agg(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Replace aggregate functions with deterministic alternatives."""

    candidates: list[MutationCandidate] = []
    select = top_level_select(tree)
    alias_map = local_alias_map(select) if select is not None else []
    numeric_column = first_visible_column(
        example.schema,
        alias_map,
        numeric_only=True,
    )

    aggregate_nodes = [
        node for node in tree.walk() if type(node) in AGGREGATE_ALTERNATIVES
    ]
    for site_index, aggregate in enumerate(aggregate_nodes):
        aggregate_type = cast(type[exp.Expression], type(aggregate))
        if aggregate_type not in AGGREGATE_ALTERNATIVES:
            continue
        replacement_types = AGGREGATE_ALTERNATIVES[aggregate_type]
        for replacement_type in replacement_types:
            mutated = clone_expression(tree)
            target_aggregates = [
                node for node in mutated.walk() if type(node) in AGGREGATE_ALTERNATIVES
            ]
            target = target_aggregates[site_index]
            argument = target.this.copy() if target.this is not None else exp.Star()
            if not isinstance(replacement_type, type):
                continue
            if isinstance(argument, exp.Star) and replacement_type is not exp.Count:
                if numeric_column is None:
                    continue
                argument = numeric_column.to_expression()
            replacement = replacement_type(this=argument)
            target.replace(replacement)
            candidates.append(
                MutationCandidate(
                    mutant_sql=serialize_sql(mutated),
                    metadata={
                        "site_index": site_index,
                        "from": aggregate.key.upper(),
                        "to": replacement.key.upper(),
                    },
                )
            )
    return candidates


def c_group_key(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Add or remove one ``GROUP BY`` key using projected columns."""

    select = top_level_select(tree)
    if select is None:
        return []

    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        if not contains_aggregate(select):
            return []
        projection_columns = select_projection_columns(select)
        if not projection_columns:
            return []
        mutated = clone_expression(select)
        mutated.set("group", exp.Group(expressions=[projection_columns[0].copy()]))
        return [
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={
                    "site_index": 0,
                    "action": "add",
                    "column": projection_columns[0].sql(),
                },
            )
        ]

    existing_keys = group_key_sqls(select)
    projection_columns = [
        column
        for column in select_projection_columns(select)
        if serialize_sql(column) not in existing_keys
    ]
    if projection_columns:
        mutated = clone_expression(select)
        target_group = mutated.args.get("group")
        if isinstance(target_group, exp.Group):
            target_group.append("expressions", projection_columns[0].copy())
            return [
                MutationCandidate(
                    mutant_sql=serialize_sql(mutated),
                    metadata={
                        "site_index": 0,
                        "action": "add",
                        "column": projection_columns[0].sql(),
                    },
                )
            ]

    if len(group.expressions) > 0:
        mutated = clone_expression(select)
        target_group = mutated.args.get("group")
        if isinstance(target_group, exp.Group):
            target_group.set("expressions", target_group.expressions[:-1])
            return [
                MutationCandidate(
                    mutant_sql=serialize_sql(mutated),
                    metadata={
                        "site_index": 0,
                        "action": "remove",
                        "column": group.expressions[-1].sql(),
                    },
                )
            ]
    return []


def c_bin_join(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Toggle eligible joins between inner and left join behavior."""

    candidates: list[MutationCandidate] = []
    eligible = matching_nodes(
        tree,
        exp.Join,
        _eligible_c_bin_join,
    )

    for site_index, _join in enumerate(eligible):
        mutated = clone_expression(tree)
        target = nth_match(
            mutated,
            exp.Join,
            site_index,
            _eligible_c_bin_join,
        )
        if not isinstance(target, exp.Join):
            continue

        current_side = target.args.get("side")
        current_kind = target.args.get("kind")
        if current_side == "LEFT":
            target.set("side", None)
            target.set("kind", "INNER")
        else:
            next_kind = None if current_kind in {None, "INNER"} else current_kind
            target.set("kind", next_kind)
            target.set("side", "LEFT")

        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={
                    "site_index": site_index,
                    "from": "LEFT" if current_side == "LEFT" else "INNER",
                    "to": "INNER" if current_side == "LEFT" else "LEFT",
                },
            )
        )
    return candidates


def _eligible_c_bin_join(current: exp.Expression) -> bool:
    """Return whether a join can participate in the binary join mutation."""

    return (
        isinstance(current, exp.Join)
        and current.args.get("on") is not None
        and current.args.get("side") in {None, "LEFT"}
        and current.args.get("kind") in {None, "INNER"}
    )


def c_sub_qual(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Rewrite simple ``IN`` and ``EXISTS`` qualification patterns."""

    candidates: list[MutationCandidate] = []
    candidates.extend(_in_to_exists(example, tree))
    candidates.extend(_exists_to_in(tree))
    return candidates


def _in_to_exists(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Rewrite recognized ``IN (subquery)`` shapes into correlated ``EXISTS``."""

    candidates: list[MutationCandidate] = []
    for site_index, node in enumerate(
        matching_nodes(
            tree,
            exp.In,
            lambda current: isinstance(current.args.get("query"), exp.Subquery),
        )
    ):
        if not isinstance(node, exp.In) or not isinstance(node.this, exp.Column):
            continue
        subquery = node.args.get("query")
        if not isinstance(subquery, exp.Subquery) or not isinstance(
            subquery.this,
            exp.Select,
        ):
            continue
        inner_select = subquery.this
        if len(inner_select.expressions) != 1 or not isinstance(
            inner_select.expressions[0],
            exp.Column,
        ):
            continue
        outer_column = _resolve_outer_in_column(example, node)
        if outer_column is None:
            continue

        mutated = clone_expression(tree)
        target = nth_match(
            mutated,
            exp.In,
            site_index,
            lambda current: isinstance(current.args.get("query"), exp.Subquery),
        )
        if not isinstance(target, exp.In):
            continue
        target_subquery = target.args.get("query")
        if not isinstance(target_subquery, exp.Subquery) or not isinstance(
            target_subquery.this,
            exp.Select,
        ):
            continue
        target_select = target_subquery.this
        inner_column = target_select.expressions[0].copy()
        target_select.set("expressions", [exp.Literal.number(1)])
        equality = exp.EQ(this=inner_column, expression=outer_column.copy())
        target_where = target_select.args.get("where")
        predicate = (
            join_conditions(target_where.this, equality)
            if isinstance(target_where, exp.Where)
            else equality
        )
        target_select.set("where", exp.Where(this=predicate))
        target.replace(exp.Exists(this=target_select))
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={"site_index": site_index, "from": "IN", "to": "EXISTS"},
            )
        )
    return candidates


def _resolve_outer_in_column(
    example: NormalizedExample,
    predicate: exp.In,
) -> exp.Column | None:
    """Resolve the outer ``IN`` column for correlation-safe ``EXISTS`` rewrites."""

    outer_column = predicate.this
    if not isinstance(outer_column, exp.Column):
        return None
    if outer_column.table:
        return outer_column.copy()

    outer_select = _enclosing_select(predicate)
    if outer_select is None:
        return None

    matches = visible_columns(
        example.schema,
        local_alias_map(outer_select),
        same_name_as=outer_column.name,
    )
    if len(matches) != 1:
        return None
    return matches[0].to_expression()


def _enclosing_select(expression: exp.Expression) -> exp.Select | None:
    """Return the closest ancestor ``SELECT`` that owns an expression."""

    current = expression.parent
    while current is not None:
        if isinstance(current, exp.Select):
            return current
        current = current.parent
    return None


def _exists_to_in(tree: exp.Expression) -> list[MutationCandidate]:
    """Rewrite simple correlated ``EXISTS`` predicates back into ``IN`` form."""

    candidates: list[MutationCandidate] = []
    exists_nodes = matching_nodes(tree, exp.Exists)
    for site_index, exists in enumerate(exists_nodes):
        if not isinstance(exists.this, exp.Select):
            continue
        inner_select = exists.this
        local_aliases = {alias for alias, _ in local_alias_map(inner_select)}
        where = inner_select.args.get("where")
        if not isinstance(where, exp.Where):
            continue

        equality, remaining = _extract_correlated_equality(where.this, local_aliases)
        if equality is None:
            continue

        outer_column, inner_column = equality
        mutated = clone_expression(tree)
        target_exists = nth_match(mutated, exp.Exists, site_index)
        if not isinstance(target_exists, exp.Exists) or not isinstance(
            target_exists.this,
            exp.Select,
        ):
            continue
        target_select = target_exists.this
        target_where = target_select.args.get("where")
        if not isinstance(target_where, exp.Where):
            continue

        target_select.set("expressions", [inner_column.copy()])
        if remaining is None:
            target_select.set("where", None)
        else:
            target_select.set("where", exp.Where(this=remaining.copy()))
        target_exists.replace(
            exp.In(
                this=outer_column.copy(),
                query=exp.Subquery(this=target_select),
            )
        )
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={"site_index": site_index, "from": "EXISTS", "to": "IN"},
            )
        )
    return candidates


def _extract_correlated_equality(
    predicate: exp.Expression,
    local_aliases: set[str],
) -> tuple[tuple[exp.Column, exp.Column] | None, exp.Expression | None]:
    """Extract one correlated equality and the remaining predicate tree."""

    conditions = split_and_conditions(predicate)
    for index, condition in enumerate(conditions):
        if not isinstance(condition, exp.EQ):
            continue
        left = condition.this
        right = condition.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        left_local = bool(left.table and left.table in local_aliases)
        right_local = bool(right.table and right.table in local_aliases)
        if left_local == right_local:
            continue
        outer_column = right if left_local else left
        inner_column = left if left_local else right
        remaining_conditions = [
            current.copy()
            for current_index, current in enumerate(conditions)
            if current_index != index
        ]
        remaining: exp.Expression | None = None
        for current in remaining_conditions:
            remaining = (
                current
                if remaining is None
                else cast(exp.Expression, exp.and_(remaining, current))
            )
        return (outer_column, inner_column), remaining
    return None, None


RULES_C = [
    MutationRule("c_pred_ror", c_pred_ror),
    MutationRule("c_pred_lcr", c_pred_lcr),
    MutationRule("c_multi_dist", c_multi_dist),
    MutationRule("c_multi_setop", c_multi_setop),
    MutationRule("c_group_agg", c_group_agg),
    MutationRule("c_group_key", c_group_key),
    MutationRule("c_bin_join", c_bin_join),
    MutationRule("c_sub_qual", c_sub_qual),
]
