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
    """Schema-backed description of one visible column in query scope."""

    alias: str
    table_name: str
    column_name: str
    column_type: str
    column_index: int

    def to_expression(self) -> exp.Column:
        """Convert the column description back into a qualified SQLGlot column."""

        return exp.column(self.column_name, table=self.alias)


def clone_expression(expression: exp.Expression) -> exp.Expression:
    """Return a detached copy of a SQLGlot expression tree."""

    return expression.copy()


def parse_sql(sql: str) -> exp.Expression:
    """Parse SQL using the repository's canonical SQLite dialect."""

    return cast(exp.Expression, parse_one(sql, read=SQL_DIALECT))


def serialize_sql(expression: exp.Expression) -> str:
    """Render a SQLGlot expression back to SQLite SQL."""

    return expression.sql(dialect=SQL_DIALECT)


def normalize_sql(sql: str) -> str:
    """Parse and reserialize SQL to obtain a canonical comparable form."""

    return serialize_sql(parse_sql(sql))


def reparses(sql: str) -> bool:
    """Return ``True`` when the given SQL parses successfully with SQLGlot."""

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
    """Collect matching nodes of one SQLGlot type, optionally filtered further."""

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
    """Return the nth matching node of a given type after optional filtering."""

    matches = matching_nodes(expression, expression_type, predicate)
    if index < 0 or index >= len(matches):
        return None
    return matches[index]


def top_level_select(expression: exp.Expression) -> exp.Select | None:
    """Return the expression when it is a top-level ``SELECT`` query."""

    return expression if isinstance(expression, exp.Select) else None


def direct_tables(select: exp.Select) -> list[tuple[str, str]]:
    """Return alias-to-table mappings visible directly in one ``SELECT`` scope."""

    tables: list[tuple[str, str]] = []

    from_clause = select.args.get("from_")
    if isinstance(from_clause, exp.From):
        tables.extend(_table_source(from_clause.this))

    for join in select.args.get("joins") or []:
        if isinstance(join, exp.Join):
            tables.extend(_table_source(join.this))

    return tables


def referenced_table_names(expression: exp.Expression) -> set[str]:
    """Return concrete table names referenced anywhere in an expression tree."""

    return {
        table.name
        for table in expression.find_all(exp.Table)
        if isinstance(table.name, str)
    }


def schema_columns(schema: NormalizedSchema) -> list[tuple[int, str, str, int]]:
    """Expand the normalized schema into per-column lookup tuples."""

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
    """Look up the normalized type for one table-qualified column."""

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
    """List schema columns visible through a query's alias map."""

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
    """Return the first visible column that matches the supplied constraints."""

    candidates = visible_columns(schema, alias_map, numeric_only=numeric_only)
    return candidates[0] if candidates else None


def group_key_sqls(select: exp.Select) -> set[str]:
    """Return canonical SQL strings for the current ``GROUP BY`` keys."""

    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return set()
    return {serialize_sql(expression) for expression in group.expressions}


def contains_aggregate(expression: exp.Expression) -> bool:
    """Return whether an expression contains one of the supported aggregates."""

    return any(
        isinstance(
            node,
            (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max),
        )
        for node in expression.walk()
    )


def split_and_conditions(expression: exp.Expression) -> list[exp.Expression]:
    """Flatten a predicate tree into top-level ``AND`` conjuncts."""

    if isinstance(expression, exp.And):
        return split_and_conditions(expression.this) + split_and_conditions(
            expression.expression
        )
    return [expression]


def join_conditions(
    left: exp.Expression | None,
    right: exp.Expression,
) -> exp.Expression:
    """Append a predicate to an existing condition tree with ``AND`` semantics."""

    if left is None:
        return right
    return cast(exp.Expression, exp.and_(left.copy(), right.copy()))


def literal_string(expression: exp.Expression | None) -> str | None:
    """Extract the Python string value from a SQL string literal."""

    if not isinstance(expression, exp.Literal) or not expression.is_string:
        return None
    return expression.this


def has_wildcard(value: str) -> bool:
    """Return whether a LIKE literal contains SQL wildcard characters."""

    return "%" in value or "_" in value


def local_alias_map(select: exp.Select) -> list[tuple[str, str]]:
    """Return the alias map used to resolve local columns in one scope."""

    return direct_tables(select)


def outer_columns(select: exp.Select) -> list[exp.Column]:
    """Return columns that reference aliases outside the current select scope."""

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
    """List columns visible inside a subquery after applying simple filters."""

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
    """Resolve a query column reference back to normalized schema metadata."""

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
    """Return direct projected columns from a ``SELECT`` list."""

    columns: list[exp.Column] = []
    for expression in select.expressions:
        if isinstance(expression, exp.Column):
            columns.append(expression)
    return columns


def comparison_nodes(expression: exp.Expression) -> Iterator[exp.Expression]:
    """Yield comparison operators supported by the classical mutation rules."""

    for node in expression.walk():
        if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            yield node


def _table_source(expression: exp.Expression | None) -> list[tuple[str, str]]:
    """Normalize a FROM/JOIN source into one or more local alias mappings."""

    if isinstance(expression, exp.Table):
        table_name = expression.name
        alias = expression.alias_or_name
        return [(alias, table_name)]
    if isinstance(expression, exp.Subquery):
        alias = expression.alias_or_name
        if alias:
            # Derived-table aliases participate in local scope resolution, but they
            # do not map directly back to a physical schema table.
            return [(alias, alias)]
    return []
