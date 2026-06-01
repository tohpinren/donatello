"""Canonical set A mutation rules."""

from __future__ import annotations

from sqlglot import exp

from donatello.models import NormalizedExample
from donatello.mutation.helpers import (
    clone_expression,
    column_info_for_reference,
    contains_aggregate,
    correlated_subqueries,
    first_visible_column,
    group_key_sqls,
    join_conditions,
    local_alias_map,
    local_columns_for_subquery,
    matching_nodes,
    nth_match,
    outer_columns,
    serialize_sql,
    top_level_select,
)
from donatello.mutation.profile import MutationCandidate, MutationRule


def a_null_pred(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Flip ``IS NULL`` and ``IS NOT NULL`` predicates."""

    candidates: list[MutationCandidate] = []

    for site_index, _node in enumerate(
        matching_nodes(
            tree,
            exp.Is,
            lambda current: isinstance(current.expression, exp.Null),
        )
    ):
        mutated = clone_expression(tree)
        target = nth_match(
            mutated,
            exp.Is,
            site_index,
            lambda current: isinstance(current.expression, exp.Null),
        )
        if not isinstance(target, exp.Is):
            continue

        if isinstance(target.parent, exp.Not):
            target.parent.replace(target.copy())
            candidates.append(
                MutationCandidate(
                    mutant_sql=serialize_sql(mutated),
                    metadata={
                        "site_index": site_index,
                        "from": "IS NOT NULL",
                        "to": "IS NULL",
                    },
                )
            )
        else:
            target.replace(exp.Not(this=target.copy()))
            candidates.append(
                MutationCandidate(
                    mutant_sql=serialize_sql(mutated),
                    metadata={
                        "site_index": site_index,
                        "from": "IS NULL",
                        "to": "IS NOT NULL",
                    },
                )
            )

    return candidates


def a_topk_ties(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Mutate ordered top-k behavior by adjusting or removing ``LIMIT``."""

    select = top_level_select(tree)
    if select is None or not isinstance(select.args.get("order"), exp.Order):
        return []

    candidates: list[MutationCandidate] = []
    limit = select.args.get("limit")
    if isinstance(limit, exp.Limit) and isinstance(limit.expression, exp.Literal):
        try:
            current_limit = int(limit.expression.this)
        except ValueError:
            current_limit = None
        if current_limit is not None:
            increased = clone_expression(select)
            increased.set(
                "limit",
                exp.Limit(expression=exp.Literal.number(current_limit + 1)),
            )
            candidates.append(
                MutationCandidate(
                    mutant_sql=serialize_sql(increased),
                    metadata={
                        "site_index": 0,
                        "from": current_limit,
                        "to": current_limit + 1,
                    },
                )
            )

            removed = clone_expression(select)
            if removed.args.get("offset") is not None:
                removed.set("limit", exp.Limit(expression=exp.Literal.number(-1)))
            else:
                removed.set("limit", None)
            candidates.append(
                MutationCandidate(
                    mutant_sql=serialize_sql(removed),
                    metadata={"site_index": 0, "from": current_limit, "to": None},
                )
            )
    elif limit is None:
        added = clone_expression(select)
        added.set("limit", exp.Limit(expression=exp.Literal.number(1)))
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(added),
                metadata={"site_index": 0, "from": None, "to": 1},
            )
        )

    return candidates


def a_agg_edge(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Mutate counting semantics around ``COUNT(*)`` and distinct counting."""

    candidates: list[MutationCandidate] = []
    alias_map = (
        local_alias_map(select)
        if (select := top_level_select(tree)) is not None
        else []
    )
    visible_column = first_visible_column(example.schema, alias_map)

    for site_index, _count in enumerate(matching_nodes(tree, exp.Count)):
        target_column = None
        if visible_column is not None:
            target_column = visible_column.to_expression()

        mutated = clone_expression(tree)
        target = nth_match(mutated, exp.Count, site_index)
        if not isinstance(target, exp.Count):
            continue

        if isinstance(target.this, exp.Star):
            if target_column is None:
                continue
            count_column = clone_expression(mutated)
            count_target = nth_match(count_column, exp.Count, site_index)
            if isinstance(count_target, exp.Count):
                count_target.set("this", target_column.copy())
                candidates.append(
                    MutationCandidate(
                        mutant_sql=serialize_sql(count_column),
                        metadata={
                            "site_index": site_index,
                            "from": "COUNT(*)",
                            "to": "COUNT(col)",
                        },
                    )
                )

            count_distinct = clone_expression(mutated)
            distinct_target = nth_match(count_distinct, exp.Count, site_index)
            if isinstance(distinct_target, exp.Count):
                distinct_target.set(
                    "this",
                    exp.Distinct(expressions=[target_column.copy()]),
                )
                candidates.append(
                    MutationCandidate(
                        mutant_sql=serialize_sql(count_distinct),
                        metadata={
                            "site_index": site_index,
                            "from": "COUNT(*)",
                            "to": "COUNT(DISTINCT col)",
                        },
                    )
                )
            continue

        if isinstance(target.this, exp.Distinct):
            plain = clone_expression(tree)
            plain_target = nth_match(plain, exp.Count, site_index)
            if isinstance(plain_target, exp.Count) and plain_target.this.expressions:
                plain_target.set("this", plain_target.this.expressions[0].copy())
                candidates.append(
                    MutationCandidate(
                        mutant_sql=serialize_sql(plain),
                        metadata={
                            "site_index": site_index,
                            "from": "COUNT(DISTINCT col)",
                            "to": "COUNT(col)",
                        },
                    )
                )

            if target_column is not None:
                star = clone_expression(tree)
                star_target = nth_match(star, exp.Count, site_index)
                if isinstance(star_target, exp.Count):
                    star_target.set("this", exp.Star())
                    candidates.append(
                        MutationCandidate(
                            mutant_sql=serialize_sql(star),
                            metadata={
                                "site_index": site_index,
                                "from": "COUNT(DISTINCT col)",
                                "to": "COUNT(*)",
                            },
                        )
                    )
            continue

        plain_column = target.this.copy()
        to_star = clone_expression(tree)
        star_target = nth_match(to_star, exp.Count, site_index)
        if isinstance(star_target, exp.Count):
            star_target.set("this", exp.Star())
            candidates.append(
                MutationCandidate(
                    mutant_sql=serialize_sql(to_star),
                    metadata={
                        "site_index": site_index,
                        "from": "COUNT(col)",
                        "to": "COUNT(*)",
                    },
                )
            )

        to_distinct = clone_expression(tree)
        distinct_target = nth_match(to_distinct, exp.Count, site_index)
        if isinstance(distinct_target, exp.Count):
            distinct_target.set(
                "this",
                exp.Distinct(expressions=[plain_column.copy()]),
            )
            candidates.append(
                MutationCandidate(
                    mutant_sql=serialize_sql(to_distinct),
                    metadata={
                        "site_index": site_index,
                        "from": "COUNT(col)",
                        "to": "COUNT(DISTINCT col)",
                    },
                )
            )

    return candidates


def a_pred_loc(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Move a non-aggregate predicate between ``WHERE`` and ``HAVING``."""

    select = top_level_select(tree)
    if select is None:
        return []

    candidates: list[MutationCandidate] = []
    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return candidates

    group_keys = group_key_sqls(select)
    where = select.args.get("where")
    if isinstance(where, exp.Where):
        predicate = where.this
        if not contains_aggregate(predicate) and _columns_within_group_keys(
            predicate,
            group_keys,
        ):
            mutated = clone_expression(select)
            mutated.set("where", None)
            mutated_having = mutated.args.get("having")
            new_predicate = (
                join_conditions(mutated_having.this, predicate)
                if isinstance(mutated_having, exp.Having)
                else predicate.copy()
            )
            mutated.set("having", exp.Having(this=new_predicate))
            candidates.append(
                MutationCandidate(
                    mutant_sql=serialize_sql(mutated),
                    metadata={"site_index": 0, "from": "WHERE", "to": "HAVING"},
                )
            )

    having = select.args.get("having")
    if isinstance(having, exp.Having):
        predicate = having.this
        if not contains_aggregate(predicate) and _columns_within_group_keys(
            predicate,
            group_keys,
        ):
            mutated = clone_expression(select)
            mutated.set("having", None)
            mutated_where = mutated.args.get("where")
            new_predicate = (
                join_conditions(mutated_where.this, predicate)
                if isinstance(mutated_where, exp.Where)
                else predicate.copy()
            )
            mutated.set("where", exp.Where(this=new_predicate))
            candidates.append(
                MutationCandidate(
                    mutant_sql=serialize_sql(mutated),
                    metadata={
                        "site_index": len(candidates),
                        "from": "HAVING",
                        "to": "WHERE",
                    },
                )
            )

    return candidates


def a_subq_corr_drop(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Break a correlated subquery by rewriting the first outer reference."""

    candidates: list[MutationCandidate] = []
    subqueries = correlated_subqueries(tree)

    for site_index, subquery in enumerate(subqueries):
        outer_refs = outer_columns(subquery)
        if not outer_refs:
            continue
        outer_ref = outer_refs[0]
        outer_info = column_info_for_reference(
            example.schema,
            local_alias_map(top_level_select(tree) or subquery),
            outer_ref,
        )
        same_type = outer_info.column_type if outer_info is not None else None
        replacements = [
            column
            for column in local_columns_for_subquery(
                subquery,
                example.schema,
                same_type_as=same_type,
            )
            if outer_info is None
            or column.alias != outer_ref.table
            or column.column_name != outer_ref.name
        ]
        if not replacements:
            continue

        mutated = clone_expression(tree)
        target_subqueries = correlated_subqueries(mutated)
        if site_index >= len(target_subqueries):
            continue
        target_subquery = target_subqueries[site_index]
        target_outer = outer_columns(target_subquery)
        if not target_outer:
            continue
        target_outer[0].replace(replacements[0].to_expression())
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={
                    "site_index": site_index,
                    "from": outer_ref.sql(),
                    "to": replacements[0].to_expression().sql(),
                },
            )
        )

    return candidates


def _columns_within_group_keys(
    predicate: exp.Expression,
    group_keys: set[str],
) -> bool:
    """Return whether every referenced column is already a grouping key."""

    column_sqls = {serialize_sql(column) for column in predicate.find_all(exp.Column)}
    return bool(column_sqls) and column_sqls.issubset(group_keys)


RULES_A = [
    MutationRule("a_null_pred", a_null_pred),
    MutationRule("a_topk_ties", a_topk_ties),
    MutationRule("a_agg_edge", a_agg_edge),
    MutationRule("a_pred_loc", a_pred_loc),
    MutationRule("a_subq_corr_drop", a_subq_corr_drop),
]
