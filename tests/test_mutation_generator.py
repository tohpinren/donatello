from __future__ import annotations

import sqlite3
from collections.abc import Callable

import pytest
from sqlglot import parse_one

from sqlmt.models import NormalizedSchema
from sqlmt.mutation.generator import generate_mutations
from sqlmt.mutation.profile import (
    MutationCandidate,
    MutationRule,
    canonical_rules,
)
from tests.helpers import (
    make_company_schema,
    make_normalized_example,
    make_single_table_schema,
)

SchemaFactory = Callable[[], NormalizedSchema]


RULE_CASES = [
    (
        "a_null_pred",
        "SELECT e.name FROM employees e WHERE e.manager_id IS NULL",
        "SELECT e.name FROM employees e WHERE e.salary > 100",
        make_company_schema,
        make_company_schema,
    ),
    (
        "a_topk_ties",
        "SELECT e.name FROM employees e ORDER BY e.salary DESC LIMIT 5",
        "SELECT e.name FROM employees e LIMIT 5",
        make_company_schema,
        make_company_schema,
    ),
    (
        "a_agg_edge",
        "SELECT COUNT(*) FROM employees e",
        "SELECT e.name FROM employees e",
        make_company_schema,
        make_company_schema,
    ),
    (
        "a_pred_loc",
        (
            "SELECT e.department_id, COUNT(*) FROM employees e "
            "WHERE e.department_id > 1 GROUP BY e.department_id"
        ),
        "SELECT COUNT(*) FROM employees e WHERE e.salary > 100",
        make_company_schema,
        make_company_schema,
    ),
    (
        "a_subq_corr_drop",
        (
            "SELECT e.name FROM employees e WHERE EXISTS ("
            "SELECT 1 FROM departments d WHERE d.id = e.department_id)"
        ),
        (
            "SELECT e.name FROM employees e WHERE EXISTS ("
            "SELECT 1 FROM departments d WHERE d.region = 'APAC')"
        ),
        make_company_schema,
        make_company_schema,
    ),
    (
        "b_join_miss",
        "SELECT e.name FROM employees e JOIN departments d ON e.department_id = d.id",
        "SELECT e.name FROM employees e",
        make_company_schema,
        make_company_schema,
    ),
    (
        "b_from_unused_tv",
        "SELECT e.name FROM employees e",
        (
            "SELECT e.name, d.name, p.name, o.city FROM employees e "
            "JOIN departments d ON e.department_id = d.id "
            "JOIN projects p ON p.department_id = d.id "
            "JOIN offices o ON o.id = e.office_id"
        ),
        make_company_schema,
        make_company_schema,
    ),
    (
        "b_subq_scope_typo",
        (
            "SELECT e.name FROM employees e WHERE EXISTS ("
            "SELECT 1 FROM departments d JOIN projects p ON p.department_id = d.id "
            "WHERE d.manager_id = e.manager_id)"
        ),
        (
            "SELECT e.name FROM employees e WHERE EXISTS ("
            "SELECT 1 FROM departments d WHERE d.id = e.department_id)"
        ),
        make_company_schema,
        make_company_schema,
    ),
    (
        "b_like_nowildcard",
        "SELECT e.name FROM employees e WHERE e.name LIKE 'Alice'",
        "SELECT e.name FROM employees e WHERE e.name LIKE 'Ali%'",
        make_company_schema,
        make_company_schema,
    ),
    (
        "b_wildcard_wrong_op",
        "SELECT e.name FROM employees e WHERE e.name LIKE 'Ali%'",
        "SELECT e.name FROM employees e WHERE e.salary = 100",
        make_company_schema,
        make_company_schema,
    ),
    (
        "b_domain_mismatch_comp",
        (
            "SELECT e.name FROM employees e JOIN departments d "
            "ON e.department_id = d.id WHERE e.salary > d.budget"
        ),
        "SELECT e.name FROM employees e WHERE e.salary > 100",
        make_company_schema,
        make_single_table_schema,
    ),
    (
        "c_pred_ror",
        "SELECT e.name FROM employees e WHERE e.salary > 100",
        "SELECT e.name FROM employees e",
        make_company_schema,
        make_company_schema,
    ),
    (
        "c_pred_lcr",
        ("SELECT e.name FROM employees e WHERE e.salary > 100 AND e.department_id > 1"),
        "SELECT e.name FROM employees e WHERE e.salary > 100",
        make_company_schema,
        make_company_schema,
    ),
    (
        "c_multi_dist",
        "SELECT e.name FROM employees e",
        "SELECT e.name FROM employees e UNION SELECT d.name FROM departments d",
        make_company_schema,
        make_company_schema,
    ),
    (
        "c_multi_setop",
        "SELECT e.name FROM employees e UNION SELECT d.name FROM departments d",
        "SELECT e.name FROM employees e",
        make_company_schema,
        make_company_schema,
    ),
    (
        "c_group_agg",
        "SELECT COUNT(*) FROM employees e",
        "SELECT e.name FROM employees e",
        make_company_schema,
        make_company_schema,
    ),
    (
        "c_group_key",
        (
            "SELECT e.department_id, e.office_id, COUNT(*) FROM employees e "
            "GROUP BY e.department_id"
        ),
        "SELECT COUNT(*) FROM employees e",
        make_company_schema,
        make_company_schema,
    ),
    (
        "c_bin_join",
        "SELECT e.name FROM employees e JOIN departments d ON e.department_id = d.id",
        "SELECT e.name FROM employees e",
        make_company_schema,
        make_company_schema,
    ),
    (
        "c_sub_qual",
        (
            "SELECT e.name FROM employees e WHERE e.department_id IN ("
            "SELECT d.id FROM departments d WHERE d.region = 'APAC')"
        ),
        "SELECT e.name FROM employees e WHERE e.department_id IN (1, 2, 3)",
        make_company_schema,
        make_company_schema,
    ),
]


def _rule(rule_id: str) -> MutationRule:
    for rule in canonical_rules():
        if rule.rule_id == rule_id:
            return rule
    raise AssertionError(f"Unknown rule: {rule_id}")


@pytest.mark.parametrize(
    ("rule_id", "positive_sql", "negative_sql", "positive_schema", "negative_schema"),
    RULE_CASES,
    ids=[case[0] for case in RULE_CASES],
)
def test_each_canonical_rule_generates_a_valid_mutant(
    rule_id: str,
    positive_sql: str,
    negative_sql: str,
    positive_schema: SchemaFactory,
    negative_schema: SchemaFactory,
) -> None:
    example = make_normalized_example(positive_sql, schema=positive_schema())

    records = generate_mutations([example], rules=[_rule(rule_id)])

    generated = [record for record in records if record.status == "generated"]

    assert generated, f"{rule_id} did not generate any mutants for its positive fixture"
    for record in generated:
        assert record.rule == rule_id
        assert record.profile == "canonical"
        assert record.mutant_sql is not None
        assert record.mutant_sql != example.gold_sql
        assert isinstance(record.metadata, dict)
        assert "site_index" in record.metadata
        parse_one(record.mutant_sql, read="sqlite")

    negative_example = make_normalized_example(negative_sql, schema=negative_schema())

    negative_records = generate_mutations([negative_example], rules=[_rule(rule_id)])

    assert [record.status for record in negative_records] == ["skipped"]
    assert negative_records[0].rule == rule_id
    assert negative_records[0].skip_reason == "rule_not_applicable"
    assert negative_records[0].mutant_sql is None


def test_parse_failure_emits_skipped_rows_for_all_canonical_rules() -> None:
    example = make_normalized_example("SELECT FROM", schema=make_company_schema())

    records = generate_mutations([example])

    assert len(records) == 19
    assert {record.rule for record in records} == {
        rule.rule_id for rule in canonical_rules()
    }
    assert all(record.status == "skipped" for record in records)
    assert all(record.skip_reason == "parse_error" for record in records)


def test_duplicate_mutants_are_recorded_explicitly() -> None:
    example = make_normalized_example(
        "SELECT e.name FROM employees e WHERE e.name LIKE 'Alice'",
        schema=make_company_schema(),
    )

    records = generate_mutations([example])

    relevant = [
        record
        for record in records
        if record.rule in {"b_like_nowildcard", "b_wildcard_wrong_op"}
    ]

    assert any(record.status == "generated" for record in relevant)
    assert any(
        record.status == "skipped" and record.skip_reason == "duplicate_sql"
        for record in relevant
    )


def test_same_as_gold_is_skipped_explicitly() -> None:
    example = make_normalized_example("SELECT e.name FROM employees e")
    rule = MutationRule(
        rule_id="test_same_as_gold",
        apply=lambda example, tree: [
            MutationCandidate(
                mutant_sql=example.gold_sql,
                metadata={"site_index": 0},
            )
        ],
    )

    records = generate_mutations([example], rules=[rule], profile="test")

    assert len(records) == 1
    assert records[0].status == "skipped"
    assert records[0].skip_reason == "same_as_gold"


def test_invalid_mutant_sql_is_skipped_explicitly() -> None:
    example = make_normalized_example("SELECT e.name FROM employees e")
    rule = MutationRule(
        rule_id="test_invalid_sql",
        apply=lambda example, tree: [
            MutationCandidate(
                mutant_sql="SELECT FROM",
                metadata={"site_index": 0},
            )
        ],
    )

    records = generate_mutations([example], rules=[rule], profile="test")

    assert len(records) == 1
    assert records[0].status == "skipped"
    assert records[0].skip_reason == "invalid_mutant_sql"


def test_mutation_generation_caps_each_rule_at_three_generated_mutants() -> None:
    example = make_normalized_example("SELECT 99")
    rule = MutationRule(
        rule_id="test_many_candidates",
        apply=lambda example, tree: [
            MutationCandidate(
                mutant_sql=f"SELECT {index}",
                metadata={"site_index": index},
            )
            for index in range(5)
        ],
    )

    records = generate_mutations([example], rules=[rule], profile="test")

    generated = [record for record in records if record.status == "generated"]
    assert len(generated) == 3


def test_mutation_generation_cap_is_overrideable() -> None:
    example = make_normalized_example("SELECT 99")
    rule = MutationRule(
        rule_id="test_many_candidates_override",
        apply=lambda example, tree: [
            MutationCandidate(
                mutant_sql=f"SELECT {index}",
                metadata={"site_index": index},
            )
            for index in range(5)
        ],
    )

    records = generate_mutations(
        [example],
        rules=[rule],
        profile="test",
        max_generated_per_rule=2,
    )

    generated = [record for record in records if record.status == "generated"]
    assert len(generated) == 2


def test_invalid_candidate_from_one_rule_does_not_stop_other_rules() -> None:
    example = make_normalized_example("SELECT e.name FROM employees e")
    bad_rule = MutationRule(
        rule_id="test_bad",
        apply=lambda example, tree: [
            MutationCandidate(mutant_sql="SELECT FROM", metadata={"site_index": 0})
        ],
    )
    good_rule = MutationRule(
        rule_id="test_good",
        apply=lambda example, tree: [
            MutationCandidate(
                mutant_sql="SELECT e.id FROM employees AS e",
                metadata={"site_index": 1},
            )
        ],
    )

    records = generate_mutations(
        [example],
        rules=[bad_rule, good_rule],
        profile="test",
    )

    assert any(
        record.rule == "test_bad" and record.skip_reason == "invalid_mutant_sql"
        for record in records
    )
    assert any(
        record.rule == "test_good" and record.status == "generated"
        for record in records
    )


def test_c_bin_join_supports_explicit_inner_join() -> None:
    example = make_normalized_example(
        "SELECT e.name FROM employees e INNER JOIN departments d "
        "ON e.department_id = d.id",
        schema=make_company_schema(),
    )

    records = generate_mutations([example], rules=[_rule("c_bin_join")])

    generated = [record for record in records if record.status == "generated"]

    assert generated
    assert any("LEFT JOIN" in (record.mutant_sql or "") for record in generated)


def test_a_topk_ties_keeps_offset_removal_sqlite_valid() -> None:
    example = make_normalized_example(
        "SELECT 1 ORDER BY 1 LIMIT 9, 2",
        schema=make_company_schema(),
    )

    records = generate_mutations([example], rules=[_rule("a_topk_ties")])

    generated = [record for record in records if record.status == "generated"]

    assert generated
    assert any(
        (record.mutant_sql or "").endswith("LIMIT -1 OFFSET 9") for record in generated
    )

    connection = sqlite3.connect(":memory:")
    try:
        for record in generated:
            assert record.mutant_sql is not None
            connection.execute(record.mutant_sql).fetchall()
    finally:
        connection.close()


@pytest.mark.parametrize(
    "rule_id",
    ["a_subq_corr_drop", "b_subq_scope_typo"],
)
def test_scope_sensitive_subquery_rules_ignore_derived_table_aliases(
    rule_id: str,
) -> None:
    example = make_normalized_example(
        (
            "SELECT e.name FROM employees e WHERE e.department_id = ("
            "SELECT d2.id FROM (SELECT d.id FROM departments d) AS d2)"
        ),
        schema=make_company_schema(),
    )

    records = generate_mutations([example], rules=[_rule(rule_id)])

    assert [record.status for record in records] == ["skipped"]
    assert records[0].skip_reason == "rule_not_applicable"


def test_c_sub_qual_qualifies_unqualified_outer_column() -> None:
    example = make_normalized_example(
        (
            "SELECT e.name FROM employees e WHERE id IN ("
            "SELECT d.id FROM departments d WHERE d.region = 'APAC')"
        ),
        schema=make_company_schema(),
    )

    records = generate_mutations([example], rules=[_rule("c_sub_qual")])

    generated = [record for record in records if record.status == "generated"]

    assert generated
    assert any("d.id = e.id" in (record.mutant_sql or "") for record in generated)
    assert all(" id = id" not in (record.mutant_sql or "") for record in generated)


def test_c_sub_qual_skips_ambiguous_unqualified_outer_column() -> None:
    example = make_normalized_example(
        (
            "SELECT e.name FROM employees e "
            "JOIN departments d ON e.department_id = d.id "
            "WHERE id IN (SELECT p.id FROM projects p)"
        ),
        schema=make_company_schema(),
    )

    records = generate_mutations([example], rules=[_rule("c_sub_qual")])

    assert [record.status for record in records] == ["skipped"]
    assert records[0].skip_reason == "rule_not_applicable"
