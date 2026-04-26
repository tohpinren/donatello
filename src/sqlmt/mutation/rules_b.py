"""Canonical set B mutation rules."""

from __future__ import annotations

from sqlglot import exp

from sqlmt.models import NormalizedExample
from sqlmt.mutation.helpers import (
    clone_expression,
    column_info_for_reference,
    comparison_nodes,
    correlated_subqueries,
    direct_tables,
    has_wildcard,
    literal_string,
    local_alias_map,
    local_columns_for_subquery,
    matching_nodes,
    nth_match,
    outer_columns,
    referenced_table_names,
    serialize_sql,
    top_level_select,
    visible_columns,
)
from sqlmt.mutation.profile import MutationCandidate, MutationRule


def b_join_miss(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Drop a join condition to simulate a missing join predicate."""

    candidates: list[MutationCandidate] = []
    for site_index, _join in enumerate(
        matching_nodes(
            tree,
            exp.Join,
            lambda current: current.args.get("on") is not None,
        )
    ):
        mutated = clone_expression(tree)
        target = nth_match(
            mutated,
            exp.Join,
            site_index,
            lambda current: current.args.get("on") is not None,
        )
        if not isinstance(target, exp.Join):
            continue
        old_on = target.args["on"].sql()
        target.set("on", None)
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={"site_index": site_index, "from": old_on, "to": None},
            )
        )
    return candidates


def b_from_unused_tv(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Add an unused table as a deterministic ``CROSS JOIN``."""

    select = top_level_select(tree)
    if select is None:
        return []

    referenced = referenced_table_names(select)
    for table_index, table_name in enumerate(example.schema.table_names_original):
        display_table_name = example.schema.table_names[table_index]
        if table_name in referenced or display_table_name in referenced:
            continue
        mutated = clone_expression(select)
        mutated.append(
            "joins",
            exp.Join(
                this=exp.Table(this=exp.to_identifier(table_name)),
                kind="CROSS",
            ),
        )
        return [
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={
                    "site_index": 0,
                    "from": None,
                    "to": table_name,
                    "to_display": display_table_name,
                },
            )
        ]
    return []


def b_subq_scope_typo(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Rewrite a correlated reference to the wrong local alias in a subquery."""

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
                same_name_as=outer_ref.name,
            )
            if column.alias != outer_ref.table
        ]
        if not replacements:
            continue

        mutated = clone_expression(tree)
        target_subqueries = correlated_subqueries(mutated)
        if site_index >= len(target_subqueries):
            continue
        target_subquery = target_subqueries[site_index]
        target_outer_refs = outer_columns(target_subquery)
        if not target_outer_refs:
            continue
        replacement = replacements[0].to_expression()
        target_outer_refs[0].replace(replacement)
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={
                    "site_index": site_index,
                    "from": outer_ref.sql(),
                    "to": replacement.sql(),
                },
            )
        )

    return candidates


def b_like_nowildcard(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Swap ``LIKE`` and ``=`` when the compared literal has no wildcard."""

    candidates: list[MutationCandidate] = []

    for site_index, _like in enumerate(
        matching_nodes(
            tree,
            exp.Like,
            lambda current: (
                (value := literal_string(current.expression)) is not None
                and not has_wildcard(value)
            ),
        )
    ):
        mutated = clone_expression(tree)
        target = nth_match(
            mutated,
            exp.Like,
            site_index,
            lambda current: (
                (value := literal_string(current.expression)) is not None
                and not has_wildcard(value)
            ),
        )
        if not isinstance(target, exp.Like):
            continue
        replacement = exp.EQ(
            this=target.this.copy(),
            expression=target.expression.copy(),
        )
        target.replace(replacement)
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={"site_index": site_index, "from": "LIKE", "to": "="},
            )
        )

    existing = len(candidates)
    for site_index, eq in enumerate(
        matching_nodes(
            tree,
            exp.EQ,
            lambda current: (
                literal_string(current.expression) is not None
                or literal_string(current.this) is not None
            ),
        )
    ):
        literal = literal_string(eq.expression) or literal_string(eq.this)
        if literal is None or has_wildcard(literal):
            continue
        mutated = clone_expression(tree)
        target = nth_match(
            mutated,
            exp.EQ,
            site_index,
            lambda current: (
                literal_string(current.expression) is not None
                or literal_string(current.this) is not None
            ),
        )
        if not isinstance(target, exp.EQ):
            continue
        replacement = exp.Like(
            this=target.this.copy(), expression=target.expression.copy()
        )
        target.replace(replacement)
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={
                    "site_index": existing + site_index,
                    "from": "=",
                    "to": "LIKE",
                },
            )
        )

    return candidates


def b_wildcard_wrong_op(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Confuse wildcard string matching with equality semantics."""

    candidates: list[MutationCandidate] = []

    for site_index, _like in enumerate(
        matching_nodes(
            tree,
            exp.Like,
            lambda current: literal_string(current.expression) is not None,
        )
    ):
        mutated = clone_expression(tree)
        target = nth_match(
            mutated,
            exp.Like,
            site_index,
            lambda current: literal_string(current.expression) is not None,
        )
        if not isinstance(target, exp.Like):
            continue
        target.replace(
            exp.EQ(
                this=target.this.copy(),
                expression=target.expression.copy(),
            )
        )
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={"site_index": site_index, "from": "LIKE", "to": "="},
            )
        )

    base_index = len(candidates)
    for site_index, _eq in enumerate(
        matching_nodes(
            tree,
            exp.EQ,
            lambda current: (
                literal_string(current.expression) is not None
                or literal_string(current.this) is not None
            ),
        )
    ):
        mutated = clone_expression(tree)
        target = nth_match(
            mutated,
            exp.EQ,
            site_index,
            lambda current: (
                literal_string(current.expression) is not None
                or literal_string(current.this) is not None
            ),
        )
        if not isinstance(target, exp.EQ):
            continue

        left = target.this.copy()
        right = target.expression.copy()
        literal = literal_string(target.expression)
        if literal is not None:
            right = exp.Literal.string(f"{literal}%")
        elif literal_string(target.this) is not None:
            left = exp.Literal.string(f"{literal_string(target.this)}%")
        else:
            continue
        target.replace(exp.Like(this=left, expression=right))
        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={
                    "site_index": base_index + site_index,
                    "from": "=",
                    "to": "LIKE",
                },
            )
        )

    return candidates


def b_domain_mismatch_comp(
    example: NormalizedExample,
    tree: exp.Expression,
) -> list[MutationCandidate]:
    """Swap one comparison side with a different same-typed visible column."""

    select = top_level_select(tree)
    if select is None:
        return []
    alias_map = direct_tables(select)

    candidates: list[MutationCandidate] = []
    for site_index, _comparison in enumerate(comparison_nodes(tree)):
        mutated = clone_expression(tree)
        target = list(comparison_nodes(mutated))[site_index]

        left = target.this
        right = target.expression
        replacement = None
        side = None

        if isinstance(left, exp.Column):
            left_info = column_info_for_reference(example.schema, alias_map, left)
            if left_info is not None:
                options = [
                    column
                    for column in visible_columns(
                        example.schema,
                        alias_map,
                        same_type_as=left_info.column_type,
                    )
                    if column.alias != left_info.alias
                ]
                if options:
                    replacement = options[0]
                    side = "left"

        if replacement is None and isinstance(right, exp.Column):
            right_info = column_info_for_reference(example.schema, alias_map, right)
            if right_info is not None:
                options = [
                    column
                    for column in visible_columns(
                        example.schema,
                        alias_map,
                        same_type_as=right_info.column_type,
                    )
                    if column.alias != right_info.alias
                ]
                if options:
                    replacement = options[0]
                    side = "right"

        if replacement is None or side is None:
            continue

        if side == "left":
            target.set("this", replacement.to_expression())
        else:
            target.set("expression", replacement.to_expression())

        candidates.append(
            MutationCandidate(
                mutant_sql=serialize_sql(mutated),
                metadata={
                    "site_index": site_index,
                    "side": side,
                    "to": replacement.to_expression().sql(),
                },
            )
        )

    return candidates


RULES_B = [
    MutationRule("b_join_miss", b_join_miss),
    MutationRule("b_from_unused_tv", b_from_unused_tv),
    MutationRule("b_subq_scope_typo", b_subq_scope_typo),
    MutationRule("b_like_nowildcard", b_like_nowildcard),
    MutationRule("b_wildcard_wrong_op", b_wildcard_wrong_op),
    MutationRule("b_domain_mismatch_comp", b_domain_mismatch_comp),
]
