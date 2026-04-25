from __future__ import annotations

from sqlmt.models import MutationRecord
from sqlmt.mutation.profile import CANONICAL_RULE_IDS, canonical_rules


def test_canonical_profile_contains_all_rules_in_order() -> None:
    assert CANONICAL_RULE_IDS == (
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


def test_every_canonical_rule_is_registered_and_constructible() -> None:
    rules = canonical_rules()

    assert [rule.rule_id for rule in rules] == list(CANONICAL_RULE_IDS)
    assert all(callable(rule.apply) for rule in rules)


def test_mutation_record_serializes_expected_row_shape() -> None:
    record = MutationRecord(
        mutant_id="bird:dev:000000:a_null_pred:00",
        example_id="bird:dev:000000",
        benchmark="bird",
        subset="dev",
        db_id="company",
        source_id="fixture-1",
        question="fixture question",
        rule="a_null_pred",
        profile="canonical",
        status="generated",
        skip_reason=None,
        gold_sql="SELECT e.name FROM employees e WHERE e.manager_id IS NULL",
        mutant_sql="SELECT e.name FROM employees AS e WHERE e.manager_id IS NOT NULL",
        sqlite_path="/tmp/company.sqlite",
        sqlite_relpath="dev/dev_databases/company/company.sqlite",
        metadata={"site_index": 0, "from": "IS NULL", "to": "IS NOT NULL"},
    )

    assert record.to_dict() == {
        "mutant_id": "bird:dev:000000:a_null_pred:00",
        "example_id": "bird:dev:000000",
        "benchmark": "bird",
        "subset": "dev",
        "db_id": "company",
        "source_id": "fixture-1",
        "question": "fixture question",
        "rule": "a_null_pred",
        "profile": "canonical",
        "status": "generated",
        "skip_reason": None,
        "gold_sql": "SELECT e.name FROM employees e WHERE e.manager_id IS NULL",
        "mutant_sql": (
            "SELECT e.name FROM employees AS e WHERE e.manager_id IS NOT NULL"
        ),
        "sqlite_path": "/tmp/company.sqlite",
        "sqlite_relpath": "dev/dev_databases/company/company.sqlite",
        "metadata": {"site_index": 0, "from": "IS NULL", "to": "IS NOT NULL"},
    }
