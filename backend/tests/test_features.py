"""Oracle feature detection: every sample object, package analysis, and edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.migration.features import (
    Handling,
    analyze_package,
    detect_features,
    literal_value,
    mask_oracle,
)
from app.parsers.oracle_parser import parse_sources

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def sample():
    sources = {p.name: p.read_text() for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    objects = {o.object_id: o for o in parse_sources(sources)}
    package = objects["package:legacy.payroll_pkg"]
    others = [o.source_code for o in objects.values() if o.object_id != package.object_id]
    return objects, {"payroll_pkg": analyze_package(package.source_code, others)}


def report_for(sample, object_id):
    objects, packages = sample
    obj = objects[object_id]
    return detect_features(obj.source_code, package=obj.package, packages=packages)


# --- the sample objects: expected findings and how each is converted ------------

EXPECTED = {
    "procedure:legacy.calc_bonus": ({"SELECT_INTO", "TYPE_ANCHOR"}, Handling.RULE),
    "procedure:legacy.apply_raise": (
        {"SELECT_INTO", "SELECT_FOR_UPDATE", "INTEGER_TYPE", "TYPE_ANCHOR", "EXCEPTION_HANDLER"}, Handling.RULE),
    "function:legacy.get_emp_grade": (
        {"SELECT_INTO", "NVL", "CONCAT", "TYPE_ANCHOR", "EXCEPTION_HANDLER"}, Handling.RULE),
    "function:legacy.top_earners": ({"ROWNUM_TOP_N", "CURSOR_FOR_LOOP", "CONCAT"}, Handling.RULE),
    "procedure:legacy.monthly_payroll": (
        {"TRUNC", "LAST_DAY", "ADD_MONTHS", "SYSDATE", "SEQUENCE", "CURSOR_FOR_LOOP", "PACKAGE_CONSTANT"},
        Handling.RULE),
    "function:legacy.payroll_pkg.net_pay": ({"NVL", "PACKAGE_VARIABLE"}, Handling.RULE),
    "procedure:legacy.payroll_pkg.close_month": (
        {"SELECT_INTO", "NVL", "TRUNC", "PACKAGE_INTERNAL_CALL"}, Handling.RULE),
    "trigger:legacy.emp_salary_audit_trg": ({"SEQUENCE", "TRIGGER_ROW_REF", "TRUNC", "SYSDATE"}, Handling.RULE),
    "view:legacy.org_chart": ({"CONNECT_BY"}, Handling.LLM),
}


@pytest.mark.parametrize("object_id", sorted(EXPECTED))
def test_sample_object_findings(sample, object_id):
    features, handling = EXPECTED[object_id]
    report = report_for(sample, object_id)
    assert report.features == features
    assert report.handling is handling
    assert report.review_reason() is None


def test_every_sample_routine_and_trigger_is_covered(sample):
    objects, _ = sample
    convertible = {k for k, o in objects.items() if o.type.value not in ("table", "sequence", "package")}
    assert convertible == set(EXPECTED)


# --- package analysis (D4) --------------------------------------------------------

def test_payroll_pkg_symbols(sample):
    pkg = sample[1]["payroll_pkg"]
    assert pkg.members == {"net_pay", "close_month"}
    allowance, tax = pkg.variables["c_allowance"], pkg.variables["g_tax_rate"]
    assert (allowance.scope, allowance.constant, allowance.initial, allowance.inlinable) == ("spec", True, "2500", True)
    assert (tax.scope, tax.constant, tax.assigned, tax.inlinable) == ("body", False, False, True)


def test_literal_value_gives_the_original_text(sample):
    objects, packages = sample
    source = objects["package:legacy.payroll_pkg"].source_code
    assert literal_value(source, packages["payroll_pkg"], "g_tax_rate") == "0.10"
    assert literal_value(source, packages["payroll_pkg"], "c_allowance") == "2500"


PKG = """
CREATE OR REPLACE PACKAGE p AS
  c_rate   CONSTANT NUMBER := 5;
  g_public NUMBER := 1;
  FUNCTION f RETURN NUMBER;
END p;
CREATE OR REPLACE PACKAGE BODY p AS
  g_counter NUMBER := 0;
  g_name    VARCHAR2(30) := 'start';
  g_into    NUMBER := 0;
  g_outarg  NUMBER := 0;
  g_calc    NUMBER := SYSDATE - 1;
  g_unused  NUMBER := 7;
  e_bad     EXCEPTION;
  FUNCTION f RETURN NUMBER IS
  BEGIN
    g_counter := g_counter + 1;
    SELECT count(*) INTO g_into FROM employees;
    fill(g_outarg);
    RETURN g_counter + g_into + g_outarg + g_calc + g_unused + c_rate;
  END f;
END p;
"""


@pytest.mark.parametrize("name, inlinable, reason", [
    ("c_rate", True, None),
    ("g_public", False, "public"),
    ("g_counter", False, "assigned"),
    ("g_into", False, "assigned"),
    ("g_outarg", False, "assigned"),
    ("g_calc", False, "not a literal"),
    ("g_unused", True, None),
])
def test_variable_visibility_and_assignment(name, inlinable, reason):
    var = analyze_package(PKG).variables[name]
    assert var.inlinable is inlinable
    if reason:
        assert reason in var.why_not_inlinable


def test_package_level_exception_is_not_a_variable():
    pkg = analyze_package(PKG)
    assert "e_bad" not in pkg.variables
    assert any(d.startswith("e_bad") for d in pkg.other_declarations)


def test_assignment_from_outside_the_package_counts():
    outside = "CREATE PROCEDURE reset IS BEGIN p.g_unused := 0; END;"
    assert analyze_package(PKG, [outside]).variables["g_unused"].assigned


def test_member_using_package_state_needs_review():
    pkg = analyze_package(PKG)
    report = detect_features(
        "FUNCTION f RETURN NUMBER IS BEGIN RETURN g_counter + c_rate; END f;", package="p", packages={"p": pkg}
    )
    assert report.handling is Handling.REVIEW
    assert "package state p.g_counter" in report.review_reason()
    assert "PACKAGE_CONSTANT" in report.features


def test_reference_to_public_package_variable_from_outside_needs_review():
    report = detect_features("BEGIN x := p.g_public; END;", packages={"p": analyze_package(PKG)})
    assert report.handling is Handling.REVIEW
    assert "public" in report.review_reason()


def test_unknown_package_item_needs_review():
    report = detect_features("BEGIN x := p.no_such; END;", packages={"p": analyze_package(PKG)})
    assert "not declared" in report.review_reason()


def test_not_a_package():
    with pytest.raises(ValueError):
        analyze_package("CREATE PROCEDURE x IS BEGIN NULL; END;")


# --- what needs a person, or Gemini ----------------------------------------------

@pytest.mark.parametrize("code, feature", [
    ("BEGIN EXECUTE IMMEDIATE 'DROP TABLE t'; END;", "DYNAMIC_SQL"),
    ("BEGIN DBMS_SQL.PARSE(c, s, 1); END;", "DYNAMIC_SQL"),
    ("IS PRAGMA AUTONOMOUS_TRANSACTION; BEGIN NULL; END;", "AUTONOMOUS_TRANSACTION"),
    ("BEGIN SELECT x INTO y FROM t@remote_db; END;", "DATABASE_LINK"),
    ("BEGIN UPDATE t SET x = 1 WHERE id = 1; COMMIT; END;", "TRANSACTION_CONTROL"),
    ("BEGIN UTL_FILE.PUT_LINE(f, 'x'); END;", "SYSTEM_PACKAGE"),
    ("SELECT a FROM t WHERE ROWNUM <= 5", "ROWNUM"),
    ("SELECT a FROM (SELECT a FROM t) WHERE ROWNUM <= 5", "ROWNUM"),  # no ORDER BY: arbitrary rows
    ("SELECT a FROM (SELECT a FROM t ORDER BY a) WHERE ROWNUM <= 5 AND a > 1", "ROWNUM"),
    ("SELECT ROWNUM, a FROM (SELECT a FROM t ORDER BY a)", "ROWNUM"),
])
def test_needs_review(code, feature):
    report = detect_features(code)
    assert feature in report.features
    assert report.handling is Handling.REVIEW
    assert report.review_reason().startswith("Unsupported: ")


@pytest.mark.parametrize("code, feature", [
    ("SELECT a FROM t1, t2 WHERE t1.id = t2.id(+)", "OUTER_JOIN_PLUS"),
    ("MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN UPDATE SET t.x = s.x", "MERGE"),
    ("IS CURSOR c IS SELECT a FROM t; BEGIN OPEN c; END;", "EXPLICIT_CURSOR"),
    ("BEGIN SELECT a BULK COLLECT INTO v FROM t; END;", "COLLECTION"),
    ("BEGIN RAISE_APPLICATION_ERROR(-20001, 'bad'); END;", "RAISE_APPLICATION_ERROR"),
    ("SELECT MONTHS_BETWEEN(a, b) FROM t", "ORACLE_FUNCTION"),
])
def test_needs_llm(code, feature):
    report = detect_features(code)
    assert feature in report.features
    assert report.handling is Handling.LLM


@pytest.mark.parametrize("code", [
    "SELECT a FROM (SELECT a FROM t ORDER BY a DESC) WHERE ROWNUM <= p_n",
    "SELECT a FROM (SELECT a FROM t ORDER BY a) x WHERE ROWNUM < 10",
    "SELECT a FROM (SELECT a FROM (SELECT a FROM s) ORDER BY a) WHERE ROWNUM <= 2.5",
])
def test_top_n_rownum_has_a_rule(code):
    report = detect_features(code)
    assert report.features == {"ROWNUM_TOP_N"}
    assert report.handling is Handling.RULE


def test_order_by_inside_a_nested_subquery_is_not_enough():
    code = "SELECT a FROM (SELECT a FROM (SELECT a FROM t ORDER BY a)) WHERE ROWNUM <= 5"
    assert detect_features(code).handling is Handling.REVIEW


# --- masking ----------------------------------------------------------------------

def test_words_in_strings_and_comments_do_not_count():
    code = """
    BEGIN
      -- EXECUTE IMMEDIATE and COMMIT in a comment
      /* ROWNUM, CONNECT BY, x@remote */
      log_it('EXECUTE IMMEDIATE ''x''; COMMIT; user@example.com');
      log_it(q'[it's ROWNUM ]' || 'done');
    END;
    """
    report = detect_features(code)
    assert report.handling is Handling.RULE
    assert report.features == {"CONCAT"}


def test_empty_string_is_kept_and_detected():
    assert mask_oracle("x := '' || 'it''s' || q'{}'") == "x := '' || 'x' || ''"
    assert "EMPTY_STRING" in detect_features("IF v = '' THEN NULL; END IF;").features


@pytest.mark.parametrize("code", ["x := 'open", "/* open", 'SELECT "open', "x := q'[open"])
def test_unreadable_source_needs_review(code):
    report = detect_features(code)
    assert report.features == {"UNREADABLE"}
    assert report.handling is Handling.REVIEW


def test_insert_into_is_not_select_into():
    report = detect_features("BEGIN INSERT INTO t (a) SELECT a FROM s; END;")
    assert "SELECT_INTO" not in report.features
