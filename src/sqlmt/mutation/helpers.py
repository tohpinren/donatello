"""Shared SQLGlot and schema helpers for mutation generation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import cast

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from sqlmt.models import NormalizedSchema

SQL_DIALECT = "sqlite"
Predicate = Callable[[exp.Expression], bool]


@dataclass(frozen=True)
class ColumnInfo:
    alias: str
    table_name: str
    column_name: str
    column_type: str
    column_index: int

    def to_expression(self) -> exp.Column:
        return exp.column(self.column_name, table=self.alias)


def clone_expression(expression: exp.Expression) -> exp.Expression:
    return expression.copy()


def parse_sql(sql: str) -> exp.Expression:
    return cast(exp.Expression, parse_one(sql, read=SQL_DIALECT))


def serialize_sql(expression: exp.Expression) -> str:
    return expression.sql(dialect=SQL_DIALECT)


def normalize_sql(sql: str) -> str:
    return serialize_sql(parse_sql(sql))


def reparses(sql: str) -> bool:
    try:
        parse_sql(sql)
    except ParseError:
        return False
    return True


def matching_nodes(
    expression: exp.Expression,
    expression_type: type[exp.Expression],
    predicate: Predicate | None = None,
) -> list[exp.Expression]:
    return [
        node
        for node in expression.find_all(expression_type)
        if predicate is None or predicate(node)
    ]


def nth_match(
    expression: exp.Expression,
    expression_type: type[exp.Expression],
    index: int,
    predicate: Predicate | None = None,
) -> exp.Expression | None:
    matches = matching_nodes(expression, expression_type, predicate)
    if index < 0 or index >= len(matches):
        return None
    return matches[index]


def top_level_select(expression: exp.Expression) -> exp.Select | None:
    return expression if isinstance(expression, exp.Select) else None


def direct_tables(select: exp.Select) -> list[tuple[str, str]]:
    tables: list[tuple[str, str]] = []

    from_clause = select.args.get("from_")
    if isinstance(from_clause, exp.From):
        tables.extend(_table_source(from_clause.this))

    for join in select.args.get("joins") or []:
        if isinstance(join, exp.Join):
            tables.extend(_table_source(join.this))

    return tables


def referenced_table_names(expression: exp.Expression) -> set[str]:
    return {
        table.name
        for table in expression.find_all(exp.Table)
        if isinstance(table.name, str)
    }


def schema_columns(schema: NormalizedSchema) -> list[tuple[int, str, str, int]]:
    columns: list[tuple[int, str, str, int]] = []
    for column_index, ((table_index, column_name), _column_type) in enumerate(
        zip(schema.column_names, schema.column_types, strict=True)
    ):
        if table_index < 0:
            continue
        table_name = schema.table_names[table_index]
        columns.append((column_index, table_name, column_name, table_index))
    return columns


def column_type(
    schema: NormalizedSchema,
    *,
    table_name: str,
    column_name: str,
) -> str | None:
    for (
        column_index,
        schema_table_name,
        schema_column_name,
        _,
    ) in schema_columns(schema):
        if schema_table_name == table_name and schema_column_name == column_name:
            return schema.column_types[column_index]
    return None


def visible_columns(
    schema: NormalizedSchema,
    alias_map: Iterable[tuple[str, str]],
    *,
    same_type_as: str | None = None,
    numeric_only: bool = False,
    same_name_as: str | None = None,
) -> list[ColumnInfo]:
    alias_items = list(alias_map)
    columns: list[ColumnInfo] = []

    for alias, table_name in alias_items:
        for column_index, schema_table_name, column_name, _ in schema_columns(schema):
            if schema_table_name != table_name:
                continue
            current_type = schema.column_types[column_index]
            if numeric_only and current_type not in {
                "integer",
                "real",
                "float",
                "double",
            }:
                continue
            if same_type_as is not None and current_type != same_type_as:
                continue
            if same_name_as is not None and column_name != same_name_as:
                continue
            columns.append(
                ColumnInfo(
                    alias=alias,
                    table_name=table_name,
                    column_name=column_name,
                    column_type=current_type,
                    column_index=column_index,
                )
            )

    return columns


def first_visible_column(
    schema: NormalizedSchema,
    alias_map: Iterable[tuple[str, str]],
    *,
    numeric_only: bool = False,
) -> ColumnInfo | None:
    candidates = visible_columns(schema, alias_map, numeric_only=numeric_only)
    return candidates[0] if candidates else None


def group_key_sqls(select: exp.Select) -> set[str]:
    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return set()
    return {serialize_sql(expression) for expression in group.expressions}


def contains_aggregate(expression: exp.Expression) -> bool:
    return any(
        isinstance(
            node,
            (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max),
        )
        for node in expression.walk()
    )


def split_and_conditions(expression: exp.Expression) -> list[exp.Expression]:
    if isinstance(expression, exp.And):
        return split_and_conditions(expression.this) + split_and_conditions(
            expression.expression
        )
    return [expression]


def join_conditions(
    left: exp.Expression | None,
    right: exp.Expression,
) -> exp.Expression:
    if left is None:
        return right
    return cast(exp.Expression, exp.and_(left.copy(), right.copy()))


def literal_string(expression: exp.Expression | None) -> str | None:
    if not isinstance(expression, exp.Literal) or not expression.is_string:
        return None
    return expression.this


def has_wildcard(value: str) -> bool:
    return "%" in value or "_" in value


def local_alias_map(select: exp.Select) -> list[tuple[str, str]]:
    return direct_tables(select)


def outer_columns(select: exp.Select) -> list[exp.Column]:
    local_aliases = {alias for alias, _ in local_alias_map(select)}
    return [
        column
        for column in select.find_all(exp.Column)
        if column.table and column.table not in local_aliases
    ]


def local_columns_for_subquery(
    select: exp.Select,
    schema: NormalizedSchema,
    *,
    same_type_as: str | None = None,
    same_name_as: str | None = None,
) -> list[ColumnInfo]:
    return visible_columns(
        schema,
        local_alias_map(select),
        same_type_as=same_type_as,
        same_name_as=same_name_as,
    )


def column_info_for_reference(
    schema: NormalizedSchema,
    alias_map: Iterable[tuple[str, str]],
    column: exp.Column,
) -> ColumnInfo | None:
    if not column.table:
        return None

    for alias, table_name in alias_map:
        if alias != column.table:
            continue
        current_type = column_type(
            schema,
            table_name=table_name,
            column_name=column.name,
        )
        if current_type is None:
            return None
        for (
            column_index,
            schema_table_name,
            schema_column_name,
            _,
        ) in schema_columns(schema):
            if schema_table_name == table_name and schema_column_name == column.name:
                return ColumnInfo(
                    alias=alias,
                    table_name=table_name,
                    column_name=column.name,
                    column_type=current_type,
                    column_index=column_index,
                )
    return None


def select_projection_columns(select: exp.Select) -> list[exp.Column]:
    columns: list[exp.Column] = []
    for expression in select.expressions:
        if isinstance(expression, exp.Column):
            columns.append(expression)
    return columns


def comparison_nodes(expression: exp.Expression) -> Iterator[exp.Expression]:
    for node in expression.walk():
        if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            yield node


def _table_source(expression: exp.Expression | None) -> list[tuple[str, str]]:
    if isinstance(expression, exp.Table):
        table_name = expression.name
        alias = expression.alias_or_name
        return [(alias, table_name)]
    return []
