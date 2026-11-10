"""Acceptance tests: the parser and analyzer emit canonical object_ids (contract 0.2).

Agreed 10 Nov 2026 (Aarsh + Tarun). PR A adds these tests; the parser does not meet
them yet, so each is marked xfail(strict=True). PR B (parser + analyzer integration)
makes them pass, and strict mode then fails the suite until the marker is removed.

Expected identities for samples/oracle, unqualified names resolved to schema "legacy".
"""

from pathlib import Path

import pytest

from app.contracts import (
    TriggerInfo,
    check_unique_object_ids,
    default_schema,
    is_object_id,
    make_object_id,
    parse_object_id,
)
from app.parsers.dependency_analyzer import analyze_dependencies
from app.parsers.oracle_parser import parse_sources

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "oracle"
SCHEMA = default_schema("legacy")

pending_pr_b = pytest.mark.xfail(strict=True, reason="PR B: parser adopts canonical object_ids")


@pytest.fixture(scope="module")
def objects():
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(SAMPLES.glob("*.sql"))}
    return parse_sources(sources)


@pytest.fixture(scope="module")
def by_id(objects):
    return {o.object_id: o for o in objects}


def _id(kind, name, package=None):
    return make_object_id(kind, name, schema=SCHEMA, package=package)


def test_sample_objects_have_no_identity_collisions(objects):
    check_unique_object_ids((o.object_id, f"{o.name} in {o.source_file}") for o in objects)


@pending_pr_b
def test_every_object_id_is_canonical_in_the_default_schema(objects):
    for obj in objects:
        ref = parse_object_id(obj.object_id)
        assert ref.kind != "unresolved", obj.object_id
        assert ref.schema == SCHEMA, obj.object_id
        assert (ref.name, ref.package) == (obj.name, obj.package), obj.object_id


@pending_pr_b
def test_every_dependency_is_a_canonical_object_id(objects):
    for obj in objects:
        bad = [d for d in obj.depends_on if not is_object_id(d)]
        assert not bad, f"{obj.object_id} depends_on {bad}"


@pending_pr_b
def test_names_params_and_tables_are_lower_case(objects):
    for obj in objects:
        texts = [obj.name, *(p.name for p in obj.params), *obj.reads_tables, *obj.writes_tables]
        assert all(t == t.lower() for t in texts), obj.object_id


@pending_pr_b
def test_package_members_are_their_own_objects(by_id):
    net_pay = by_id[_id("function", "net_pay", package="payroll_pkg")]
    close_month = by_id[_id("procedure", "close_month", package="payroll_pkg")]
    assert net_pay.package == close_month.package == "payroll_pkg"
    assert [p.name for p in close_month.params] == ["p_pay_month", "p_total"]


@pending_pr_b
def test_package_constant_reference_depends_on_the_package(by_id):
    monthly = by_id[_id("procedure", "monthly_payroll")]
    assert _id("package", "payroll_pkg") in monthly.depends_on
    assert _id("sequence", "payroll_run_seq") in monthly.depends_on


@pending_pr_b
def test_trigger_metadata_is_separate_from_its_writes(by_id):
    trigger = by_id[_id("trigger", "emp_salary_audit_trg")]
    assert trigger.trigger == TriggerInfo(table=_id("table", "employees"), timing="after",
                                          events=["update"], update_columns=["base_salary"],
                                          for_each_row=True)
    assert trigger.writes_tables == ["salary_audit"]
    assert _id("table", "employees") in trigger.depends_on


@pending_pr_b
def test_execution_order_uses_object_ids(objects):
    order = analyze_dependencies(objects).execution_order
    assert set(order) == {o.object_id for o in objects}
