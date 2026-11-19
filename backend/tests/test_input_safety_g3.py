"""PR 1 / G3: allow-list detection, three states, line numbers.

Code takes the rules route only if every construct is allow-listed. Recognised features
with no tested rule, and anything not recognised at all, go to review with the line of
their first occurrence.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.migration.allow_list import (
    ALLOWED_FUNCTIONS,
    ALLOWED_WORDS,
    BEHAVIORAL_TEST,
    EVIDENCE_KINDS,
    IN_SAMPLES,
    ORACLE_RESERVED,
    RECOGNISED,
    SAMPLE,
    TRANSLATION_TEST,
    UNRESOLVED,
)
from app.migration.features import (
    UNKNOWN_CONSTRUCT,
    Handling,
    analyze_package,
    detect_features,
    known_names,
    mask_oracle,
    semantic_risks,
)
from app.parsers.oracle_parser import parse_sources
from tests.pr1_samples import oracle_sample_sources

BACKEND = Path(__file__).resolve().parents[1]

#: The 11 constructs probed on 18 Nov (7) and in the PR 1 proposal (4): all went to rules before.
PROBES = [
    ("SELECT * FROM t PIVOT (SUM(x) FOR y IN (1, 2))", "PIVOT"),
    ("SELECT * FROM t MODEL DIMENSION BY (a) MEASURES (b) RULES ()", "MODEL"),
    ("SELECT * FROM t AS OF TIMESTAMP SYSTIMESTAMP", "FLASHBACK"),
    ("FUNCTION f RETURN t PIPELINED IS BEGIN NULL; END;", "PIPELINED"),
    ("v XMLTYPE;", "XML"),
    ("GOTO done;", "GOTO"),
    ("n := SQL%ROWCOUNT;", "IMPLICIT_CURSOR_ATTRIBUTE"),
    ("x := TO_CHAR(d, 'YYYY');", UNKNOWN_CONSTRUCT),
    ("x := SUBSTR(a, 1, 2);", UNKNOWN_CONSTRUCT),
    ("x := LPAD(a, 3);", UNKNOWN_CONSTRUCT),
    ("SELECT * FROM t WHERE ROWID = r", "ROWID"),
]


def features_of(code: str, **kwargs) -> dict[str, object]:
    return {f.feature: f for f in detect_features(code, **kwargs).findings}


@pytest.mark.parametrize(("code", "feature"), PROBES)
def test_every_probe_now_goes_to_review(code, feature):
    report = detect_features(code)
    assert report.handling is Handling.REVIEW
    finding = features_of(code)[feature]
    assert finding.line == 1
    assert finding.state == ("not_recognised" if feature == UNKNOWN_CONSTRUCT else "recognised_unsupported")


def test_probe_count_is_eleven():
    assert len(PROBES) == 11


@pytest.mark.parametrize(("feature", "pattern", "detail"), RECOGNISED)
def test_recognised_features_go_to_review_not_gemini(feature, pattern, detail):
    assert pattern.flags & re.IGNORECASE
    assert detail.endswith(("no tested rule", "(no PostgreSQL equivalent)"))


def test_the_unknown_function_names_its_line_and_text():
    code = "BEGIN\n  /* a\n     comment */\n  v := NVL(a, 0);\n  v := WIDTH_BUCKET(a, 0, 10, 5);\nEND;"
    finding = features_of(code)[UNKNOWN_CONSTRUCT]
    assert (finding.line, finding.text) == (5, "WIDTH_BUCKET(")
    assert finding.detail == "function WIDTH_BUCKET is not on the allow-list"
    assert features_of(code)["NVL"].line == 4


def test_each_finding_points_at_its_first_occurrence():
    code = "BEGIN\n  x := 1;\n  y := NVL(a, 0);\n  z := NVL(b, 0);\nEND;"
    assert features_of(code)["NVL"].line == 3


def test_lines_and_text_do_not_change_what_a_finding_is():
    report = detect_features("x := NVL(a, 0);\ny := NVL(b, 0);")
    assert [f.feature for f in report.findings] == ["NVL"]


def test_supported_code_stays_on_the_rules_route():
    code = "BEGIN\n  SELECT NVL(SUM(base_salary), 0) INTO v FROM employees WHERE dept_id = 10;\nEND;"
    report = detect_features(code)
    assert report.handling is Handling.RULE
    assert {f.state for f in report.findings} == {"supported"}


# --- context: identifiers, strings, comments, qualification ---------------------------------


@pytest.mark.parametrize("code", [
    "SELECT employee_level, employee_date FROM employees",
    'SELECT "LEVEL", "DATE" FROM t',
    "-- PIVOT ( GOTO ROWID TO_CHAR(x)\nSELECT 1 INTO v FROM employees",
    "/* MODEL DIMENSION BY */ SELECT 1 INTO v FROM employees",
    "v := 'PIVOT ( GOTO l; TO_CHAR(x) ROWID';",
    "v := q'[SQL%ROWCOUNT XMLTYPE]';",
])
def test_identifiers_strings_and_comments_are_not_constructs(code):
    assert detect_features(code).handling is Handling.RULE


def test_a_call_to_an_uploaded_routine_is_known():
    code = "BEGIN\n  v := calc_bonus(102);\nEND;"
    assert detect_features(code).handling is Handling.REVIEW
    assert detect_features(code, names={"calc_bonus"}).handling is Handling.RULE


def test_schema_qualified_calls_resolve_against_the_upload():
    names = {"legacy", "calc_bonus"}
    assert detect_features("v := legacy.calc_bonus(1);", names=names).handling is Handling.RULE
    finding = features_of("v := hr.calc(1);", names=names)[UNKNOWN_CONSTRUCT]
    assert finding.detail == "call to hr.calc, which is not in the upload or the allow-list"


def test_package_qualified_calls_use_the_package_symbols():
    spec = (
        "CREATE OR REPLACE PACKAGE pay AS FUNCTION net(p NUMBER) RETURN NUMBER; END pay;\n"
        "CREATE OR REPLACE PACKAGE BODY pay AS\n"
        "  FUNCTION net(p NUMBER) RETURN NUMBER IS BEGIN RETURN p; END net;\nEND pay;"
    )
    packages = {"pay": analyze_package(spec)}
    assert detect_features("v := pay.net(1);", packages=packages).handling is Handling.RULE
    assert "PACKAGE_UNKNOWN" in features_of("v := pay.gross(1);", packages=packages)


def test_overloaded_calls_to_a_known_routine_are_known():
    code = "BEGIN\n  calc(1);\n  calc(1, 'A');\nEND;"
    assert detect_features(code, names={"calc"}).handling is Handling.RULE


def test_sql_nested_in_plsql_is_checked_with_its_line():
    code = (
        "BEGIN\n  FOR r IN (SELECT * FROM t\n"
        "            PIVOT (SUM(x) FOR y IN (1))) LOOP\n    NULL;\n  END LOOP;\nEND;"
    )
    finding = features_of(code)["PIVOT"]
    assert finding.line == 3


def test_dynamic_sql_stays_review_and_its_string_is_not_scanned():
    report = detect_features("BEGIN\n  EXECUTE IMMEDIATE 'SELECT TO_CHAR(x) FROM t PIVOT (y)';\nEND;")
    assert report.handling is Handling.REVIEW
    assert {f.feature for f in report.findings} == {"DYNAMIC_SQL"}


def test_declared_names_are_not_unknown_calls():
    code = (
        "DECLARE\n"
        "  TYPE t_tab IS TABLE OF NUMBER INDEX BY PLS_INTEGER;\n"
        "  v_tab t_tab;\n"
        "  CURSOR c_emp(p NUMBER) IS SELECT 1 FROM employees;\n"
        "BEGIN\n"
        "  v_tab(1) := 2;\n"
        "  FOR rec IN c_emp(1) LOOP NULL; END LOOP;\n"
        "END;"
    )
    assert UNKNOWN_CONSTRUCT not in features_of(code)


def test_table_and_column_lists_are_not_calls():
    code = "INSERT INTO payroll_runs (run_id, emp_id) VALUES (1, 2);"
    assert detect_features(code).handling is Handling.RULE


def test_explicit_cursor_attributes_are_gemini_eligible_and_sql_ones_are_review():
    found = features_of("DECLARE CURSOR c IS SELECT 1 FROM t; BEGIN OPEN c; IF c%NOTFOUND THEN NULL; END IF; END;")
    assert found["CURSOR_ATTRIBUTE"].handling is Handling.LLM
    assert features_of("n := SQL%ROWCOUNT;")["IMPLICIT_CURSOR_ATTRIBUTE"].handling is Handling.REVIEW


def test_an_unknown_attribute_is_unknown():
    assert features_of("x := v%CHARSETID;")[UNKNOWN_CONSTRUCT].text == "%CHARSETID"


def test_a_reserved_word_that_is_not_allowed_is_unknown():
    finding = features_of("BEGIN\n  LOCK TABLE employees IN EXCLUSIVE MODE;\nEND;")[UNKNOWN_CONSTRUCT]
    assert finding.line == 2 and finding.detail.startswith("reserved word ")


def test_reserved_words_reported_by_a_feature_are_not_reported_twice():
    report = detect_features("SELECT a FROM t MINUS SELECT a FROM u")
    assert {f.feature for f in report.findings} == {"MINUS"}


# --- the allow-list itself -----------------------------------------------------------------


def test_allowed_words_are_reserved_words_and_functions_are_not():
    assert set(ALLOWED_WORDS) <= ORACLE_RESERVED
    assert not set(ALLOWED_FUNCTIONS) & ORACLE_RESERVED


def _sample_code() -> str:
    objects = parse_sources(oracle_sample_sources())
    return " ".join(mask_oracle(o.source_code) for o in objects if o.type.value not in ("table", "sequence"))


# --- the allow-list's evidence (Aarsh, 19 Nov) ----------------------------------------------
# SAMPLE: used in the sample corpus. TRANSLATION_TEST: a named test pins the emitted SQL.
# BEHAVIORAL_TEST: Oracle and PostgreSQL results compared (PR 1b). Translation evidence is
# never treated as semantic-equivalence evidence.

_SKIPS = {"skip", "skipif", "xfail", "importorskip"}


def _names_in(node: ast.AST) -> set[str]:
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(node) if isinstance(n, ast.Name)
    }


def _strings_in(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _check_cited_test(ref: str, construct: re.Pattern) -> None:
    """A TRANSLATION_TEST citation names one test that exists, is not skipped, asserts and
    uses the construct (in its own strings, its parametrize list or a module-level case table
    it reads). test_cited_translation_tests_run_and_pass checks that it runs and passes."""
    path, _, name = ref.partition("::")
    assert re.fullmatch(r"tests/test_\w+\.py", path) and name.startswith("test_"), f"{ref}: not a test reference"
    assert path != "tests/test_input_safety_g3.py", f"{ref}: cannot cite the evidence check itself"
    file = BACKEND / path
    assert file.is_file(), f"{ref}: no such file"
    module = ast.parse(file.read_text(encoding="utf-8"))
    tests = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    assert name in tests, f"{ref}: no such test"
    test = tests[name]
    assert not _names_in(test) & _SKIPS, f"{ref}: skipped or expected to fail"
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(getattr(t, "id", None) == "pytestmark" for t in statement.targets):
            assert not _names_in(statement.value) & _SKIPS, f"{ref}: the whole file is skipped"
    assert any(isinstance(n, ast.Assert) for n in ast.walk(test)), f"{ref}: asserts nothing"
    used = _names_in(test)
    strings = _strings_in(test)
    for statement in module.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if any(getattr(t, "id", None) in used for t in targets):
                strings += _strings_in(statement.value)
    assert construct.search(" ".join(strings).upper()), f"{ref}: does not use {construct.pattern}"


def _check_evidence(entry: str, construct: re.Pattern, evidence: tuple) -> None:
    assert evidence, f"{entry}: no evidence"
    for item in evidence:
        assert item.kind in EVIDENCE_KINDS, f"{entry}: unknown evidence kind {item.kind!r}"
        if item.kind == SAMPLE:
            assert item == IN_SAMPLES
            assert construct.search(_sample_code().upper()), f"{entry}: not used in the samples"
        elif item.kind == TRANSLATION_TEST:
            _check_cited_test(item.ref, construct)
        else:
            pytest.fail(f"{entry}: BEHAVIORAL_TEST needs Oracle-recorded cases (PR 1b); none can be claimed in PR 1")


@pytest.mark.parametrize("name", sorted(ALLOWED_FUNCTIONS))
def test_every_allowed_function_has_evidence(name):
    _check_evidence(name, re.compile(rf"\b{name}\s*\("), ALLOWED_FUNCTIONS[name])


@pytest.mark.parametrize("word", sorted(ALLOWED_WORDS))
def test_every_allowed_word_has_evidence(word):
    _check_evidence(word, re.compile(rf"\b{word}\b"), ALLOWED_WORDS[word])


def _cited(kind: str) -> set[str]:
    return {e.ref for ev in (*ALLOWED_FUNCTIONS.values(), *ALLOWED_WORDS.values()) for e in ev if e.kind == kind}


def test_cited_translation_tests_run_and_pass(tmp_path):
    """Every cited TRANSLATION_TEST is collected, runs (no skip) and passes, offline too."""
    refs = sorted(_cited(TRANSLATION_TEST))
    assert refs
    report = tmp_path / "cited.xml"
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}", *refs],
        cwd=BACKEND, capture_output=True, text=True, timeout=600, check=False,
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    ran = set()
    for case in ET.parse(report).getroot().iter("testcase"):
        test = f"{case.get('classname').replace('.', '/')}.py::{case.get('name').split('[')[0]}"
        for outcome in ("skipped", "failure", "error"):
            assert case.find(outcome) is None, f"{test}: {outcome}"
        ran.add(test)
    assert set(refs) <= ran, sorted(set(refs) - ran)


def test_no_behavioural_evidence_is_claimed_before_pr1b():
    assert not _cited(BEHAVIORAL_TEST)


def test_greatest_null_semantics_stay_unresolved():
    # Aarsh, 19 Nov: unresolved and excluded from unrestricted approval until a NULL-safe rule
    # and behavioural tests exist. Not unrestricted means: every use is flagged for human
    # sign-off (the full path is in test_unresolved_semantics.py).
    assert "GREATEST" in UNRESOLVED
    assert "NULL" in UNRESOLVED["GREATEST"]
    for name in UNRESOLVED:
        risks = semantic_risks(f"BEGIN x := {name}(a, b); END;")
        assert [r.function for r in risks] == [name], f"{name}: a use is not flagged"
        warning = risks[0].warning("function:legacy.f")
        assert warning.startswith("Needs review: ") and "Semantic equivalence is not verified" in warning
    for name in UNRESOLVED:
        assert name in ALLOWED_FUNCTIONS or name in ALLOWED_WORDS
        evidence = ALLOWED_FUNCTIONS.get(name, ALLOWED_WORDS.get(name))
        assert all(e.kind != BEHAVIORAL_TEST for e in evidence), f"{name}: resolved? remove it from UNRESOLVED"


# --- the sample corpus keeps its routes ----------------------------------------------------


def test_sample_objects_route_exactly_as_before():
    objects = parse_sources(oracle_sample_sources())
    packages = {o.name: analyze_package(o.source_code, [x.source_code for x in objects if x is not o])
                for o in objects if o.type.value == "package"}
    names = known_names(objects)
    routes = {
        o.object_id: detect_features(o.source_code, package=o.package, packages=packages, names=names).handling
        for o in objects if o.type.value not in ("table", "sequence", "package")
    }
    assert routes == {
        "function:legacy.get_emp_grade": Handling.RULE,
        "function:legacy.payroll_pkg.net_pay": Handling.RULE,
        "function:legacy.top_earners": Handling.RULE,
        "procedure:legacy.apply_raise": Handling.RULE,
        "procedure:legacy.calc_bonus": Handling.RULE,
        "procedure:legacy.monthly_payroll": Handling.RULE,
        "procedure:legacy.payroll_pkg.close_month": Handling.RULE,
        "trigger:legacy.emp_salary_audit_trg": Handling.RULE,
        "view:legacy.org_chart": Handling.LLM,
    }


def test_known_names_cover_objects_members_and_schemas():
    names = known_names(parse_sources(oracle_sample_sources()))
    assert {"calc_bonus", "payroll_pkg", "net_pay", "payroll_pkg.net_pay", "legacy", "employees"} <= names


def test_position_keeping_mask_keeps_every_offset_and_line():
    source = "a := 'it''s\nok'; -- note\nb := q'[x]'; c := ''; /* x\ny */ d"
    masked = mask_oracle(source, keep_positions=True)
    assert len(masked) == len(source)
    assert masked.count("\n") == source.count("\n")
    assert masked.index(" d") == source.index(" d")
    assert "note" not in masked and "ok" not in masked and "''" in masked


def test_collections_and_explicit_cursors_keep_their_gemini_route():
    collection = "DECLARE TYPE t IS TABLE OF NUMBER INDEX BY PLS_INTEGER; v t; BEGIN v(1) := 1; END;"
    cursor = "DECLARE CURSOR c IS SELECT 1 FROM t; r NUMBER; BEGIN OPEN c; FETCH c INTO r; CLOSE c; END;"
    assert detect_features(collection).handling is Handling.LLM
    assert detect_features(cursor).handling is Handling.LLM


def test_an_unknown_sql_attribute_is_unknown_and_reported_once():
    report = detect_features("x := SQL%FOO;")
    assert [(f.feature, f.text) for f in report.findings] == [(UNKNOWN_CONSTRUCT, "%FOO")]


def test_system_package_calls_keep_their_own_finding_only():
    report = detect_features("BEGIN\n  DBMS_OUTPUT.PUT_LINE('x');\nEND;")
    assert report.handling is Handling.LLM
    assert {f.feature for f in report.findings} == {"DBMS_OUTPUT"}
    assert {f.feature for f in detect_features("UTL_FILE.FCLOSE(f);").findings} == {"SYSTEM_PACKAGE"}


# --- review fix 1 (Aarsh, 18 Nov): a known qualifier must not let an unknown call through ---


def _unknown_texts(code: str, **kwargs) -> list[str]:
    return [f.text for f in detect_features(code, **kwargs).findings if f.feature == UNKNOWN_CONSTRUCT]


def test_reported_case_known_schema_does_not_admit_an_unknown_function():
    code = "CREATE PROCEDURE p AS BEGIN APP.MYSTERY_FUNCTION(); END;"
    report = detect_features(code, names={"app"})
    assert report.handling is Handling.REVIEW
    assert _unknown_texts(code, names={"app"}) == ["APP.MYSTERY_FUNCTION("]


@pytest.mark.parametrize(("code", "names", "expected"), [
    ("v := app.mystery(1);", {"app"}, ["app.mystery("]),
    ("v := APP.PKG.MYSTERY(1);", {"app", "pkg"}, ["APP.PKG.MYSTERY("]),
    ('v := "APP"."MYSTERY"(1);', {"app"}, ['"APP"."MYSTERY"(']),
    ('v := "APP".mystery(1);', {"app"}, ['"APP".mystery(']),
    ("v := legacy.payroll_pkg.net_pay(1);", None, ["legacy.payroll_pkg.net_pay("]),
    ("v := a.b.c.d(1);", {"a", "b", "c", "d"}, ["a.b.c.d("]),
])
def test_unknown_qualified_calls_are_reported_as_a_whole_as_written(code, names, expected):
    assert _unknown_texts(code, names=names) == expected


@pytest.mark.parametrize(("code", "names"), [
    ("v := legacy.calc_bonus(1);", {"legacy", "calc_bonus"}),
    ('v := "LEGACY"."CALC_BONUS"(1);', {"legacy", "calc_bonus"}),
    ("v := payroll_pkg.net_pay(1);", {"payroll_pkg.net_pay"}),
    ("v := legacy.payroll_pkg.net_pay(1);", {"legacy", "payroll_pkg.net_pay"}),
    ("INSERT INTO app.payroll_runs (run_id) VALUES (1);", {"app"}),
    ("DBMS_OUTPUT.PUT_LINE('x');", None),
    ("DECLARE TYPE t IS TABLE OF NUMBER; v t := t(); BEGIN v.EXTEND(1); END;", None),
])
def test_resolvable_qualified_calls_are_not_unknown(code, names):
    assert _unknown_texts(code, names=names) == []


def _pay_packages():
    spec = (
        "CREATE OR REPLACE PACKAGE pay AS FUNCTION net(p NUMBER) RETURN NUMBER; END pay;\n"
        "CREATE OR REPLACE PACKAGE BODY pay AS\n"
        "  FUNCTION net(p NUMBER) RETURN NUMBER IS BEGIN RETURN p; END net;\nEND pay;"
    )
    return {"pay": analyze_package(spec)}


def test_schema_qualified_package_members_are_checked_against_the_package():
    packages = _pay_packages()
    assert _unknown_texts("v := legacy.pay.net(1);", packages=packages, names={"legacy"}) == []
    assert _unknown_texts("v := legacy.pay.gross(1);", packages=packages, names={"legacy"}) == ["legacy.pay.gross("]
    assert _unknown_texts("v := other.pay.net(1);", packages=packages, names={"legacy"}) == ["other.pay.net("]


def test_two_part_package_members_are_still_reported_once_by_the_package_check():
    report = detect_features("v := pay.gross(1);", packages=_pay_packages())
    assert [f.feature for f in report.findings] == ["PACKAGE_UNKNOWN"]


# --- review fix 3 (Aarsh, 18 Nov): quoted calls fail closed, with Oracle's case rules ------
# Unquoted names fold to upper case; quoted names are exact. So "MYSTERY" is the same as
# MYSTERY or mystery, but "mystery" is a different identifier.


def test_reported_case_an_unknown_quoted_call_is_unknown():
    assert detect_features('v := "MYSTERY"(1);').handling is Handling.REVIEW
    assert _unknown_texts('v := "MYSTERY"(1);') == ['"MYSTERY"(']


def test_a_lower_case_quoted_call_is_unknown_unless_that_exact_identifier_is_known():
    # The upload's mystery (unquoted) is MYSTERY; "mystery" is a different identifier.
    assert _unknown_texts('v := "mystery"(1);', names={"mystery"}) == ['"mystery"(']
    assert _unknown_texts('v := "MYSTERY"(1);', names={"mystery"}) == []


def test_a_quoted_call_to_a_declared_function_is_known_with_its_exact_case():
    code = (
        'DECLARE\n  FUNCTION "Fmt"(p NUMBER) RETURN NUMBER IS BEGIN RETURN p; END;\n'
        'BEGIN\n  v := "Fmt"(1);\n  w := "FMT"(1);\nEND;'
    )
    assert _unknown_texts(code) == ['"FMT"(']


@pytest.mark.parametrize("code", [
    '-- v := "MYSTERY"(1);\nv := 1;',
    '/* "MYSTERY"(1) */ v := 1;',
    "v := 'calls \"MYSTERY\"(1) later';",
    "v := q'[\"MYSTERY\"(1)]';",
])
def test_quoted_calls_in_comments_and_strings_are_ignored(code):
    assert _unknown_texts(code) == []


def test_quoted_names_follow_the_same_rules_inside_qualified_calls():
    names = {"app", "calc_bonus"}
    assert _unknown_texts('v := "APP"."CALC_BONUS"(1);', names=names) == []
    assert _unknown_texts('v := "app"."CALC_BONUS"(1);', names=names) == ['"app"."CALC_BONUS"(']


def test_quoted_built_ins_follow_the_same_rules():
    assert _unknown_texts('v := "NVL"(a, 0);') == []
    assert _unknown_texts('v := "nvl"(a, 0);') == ['"nvl"(']


def test_unquoted_names_stay_case_insensitive():
    assert _unknown_texts("v := Calc_Bonus(1);", names={"calc_bonus"}) == []
    assert _unknown_texts("v := CALC_BONUS(1);", names={"calc_bonus"}) == []


def test_quoted_names_that_are_not_calls_are_still_names():
    assert _unknown_texts('SELECT "LEVEL", "DATE" INTO a, b FROM t;') == []
    assert _unknown_texts('INSERT INTO "PAYROLL_RUNS" ("RUN_ID") VALUES (1);') == []


# --- qualified calls on declared names and system packages (Aarsh, 19 Nov) ----------------

_COLLECTION = "DECLARE\n  TYPE t IS TABLE OF NUMBER;\n  v t := t();\nBEGIN\n  {stmt}\nEND;"


def _unknown(source: str, names=()) -> list[str]:
    return [f.detail for f in detect_features(source, names=set(names)).findings if f.feature == UNKNOWN_CONSTRUCT]


@pytest.mark.parametrize("stmt", [
    "v.EXTEND(1);", "v.EXTEND(2, 1);", "IF v.EXISTS(1) THEN NULL; END IF;", "v.DELETE(1);", "v.TRIM(1);",
    "x := v.PRIOR(2);", "x := v.NEXT(1);", "x := v.COUNT;", "x := v.FIRST;", "v.DELETE;",
])
def test_collection_methods_on_a_declared_name_are_accepted(stmt):
    assert _unknown(_COLLECTION.format(stmt=stmt)) == []


@pytest.mark.parametrize("stmt, call", [
    ("v.unknown_method(1);", "v.unknown_method"),
    ("x := v.to_json(1);", "v.to_json"),
    ("v.extend.more(1);", "v.extend.more"),               # three parts: never a collection method
    ("v.inner.EXTEND(1);", "v.inner.EXTEND"),             # a field's method: fails closed (review)
    ('v."MYSTERY"(1);', 'v."MYSTERY"'),
])
def test_other_calls_on_a_declared_name_are_unknown(stmt, call):
    assert _unknown(_COLLECTION.format(stmt=stmt)) == [f"call to {call}, which is not in the upload or the allow-list"]


def test_a_call_on_a_cursor_loop_record_is_unknown():
    source = "BEGIN FOR r IN (SELECT 1 AS a FROM dual) LOOP x := r.compute(1); END LOOP; END;"
    assert _unknown(source) == ["call to r.compute, which is not in the upload or the allow-list"]


@pytest.mark.parametrize("source, call", [
    ("BEGIN x := my_util.utl_thing(1); END;", "my_util.utl_thing"),   # UTL_ as the called name
    ("BEGIN x := app.dbms_fake(1); END;", "app.dbms_fake"),
    ('BEGIN "DBMS_X".run(1); END;', '"DBMS_X".run'),                   # quoted: no system finding
])
def test_a_system_package_name_in_the_wrong_place_is_unknown(source, call):
    assert _unknown(source, names={"app"}) == [f"call to {call}, which is not in the upload or the allow-list"]
    assert detect_features(source, names={"app"}).handling is Handling.REVIEW


@pytest.mark.parametrize("source, feature, handling", [
    ("BEGIN DBMS_OUTPUT.PUT_LINE('x'); END;", "DBMS_OUTPUT", Handling.LLM),
    ("BEGIN SYS.DBMS_LOCK.SLEEP(1); END;", "SYSTEM_PACKAGE", Handling.REVIEW),
    ("BEGIN x := app.dbms_fake.run(1); END;", "SYSTEM_PACKAGE", Handling.REVIEW),
    ("BEGIN UTL_FILE.FCLOSE(f); END;", "SYSTEM_PACKAGE", Handling.REVIEW),
])
def test_real_system_package_calls_keep_their_own_finding(source, feature, handling):
    report = detect_features(source, names={"app"})
    assert feature in report.features and report.handling is handling
    assert _unknown(source, names={"app"}) == []


def test_the_outer_join_marker_is_not_a_call():
    report = detect_features("SELECT a FROM t1, t2 WHERE t1.id = t2.id (+)")
    assert report.features == {"OUTER_JOIN_PLUS"} and report.handling is Handling.LLM


def test_national_q_quotes_are_masked_with_positions_kept():
    source = "x := nq'[it's]' || NQ'{a}' || q'(b)';"
    masked = mask_oracle(source, keep_positions=True)
    assert len(masked) == len(source) and "it" not in masked and "'s" not in masked
    assert masked == "x := n'xxxxxxx' || N'xxxx' || 'xxxx';"  # the n stays, the q-string is blanked


# --- q-quoted content never counts (Aarsh, 19 Nov): no finding, no semantic-risk warning ---

#: Code that would be found if it were not inside a string: a review feature, an LLM feature,
#: an unknown function, a reserved word not allowed, a system package and GREATEST.
_LOUD = "EXECUTE IMMEDIATE x; PIVOT (a) mystery_fn(1) MINUS ROWID DBMS_LOCK.SLEEP(1) GREATEST(a, b)"


@pytest.mark.parametrize("opening, closing", [
    ("q'[", "]'"), ("Q'{", "}'"), ("q'<", ">'"), ("q'(", ")'"), ("q'!", "!'"), ("q'#", "#'"),
    ("nq'[", "]'"), ("NQ'{", "}'"), ("Nq'!", "!'"),
])
@pytest.mark.parametrize("content", [
    _LOUD,
    f"line one\n{_LOUD}\n  -- not a comment, it's text\nlast ] line",
    f"'starts with a quote {_LOUD}",            # q'!'...!' closes at !' only, never at the opening !'
])
def test_q_quoted_content_is_never_detected(opening, closing, content):
    assert closing not in content  # only the closing pair ends a q-quote; a lone ] or ) does not
    source = f"BEGIN\n  v := {opening}{content}{closing};\n  w := GREATEST(c, d);\nEND;"
    masked = mask_oracle(source, keep_positions=True)
    assert len(masked) == len(source) and masked.count("\n") == source.count("\n")
    literal = masked[masked.index(":= ") + 3:masked.index(";\n  w")]
    # The whole literal, delimiters included, is blanked: only quotes, x, line breaks (and the n of nq).
    assert set(literal) <= {"'", "x", "\n", "n", "N"}, literal
    report = detect_features(source)
    assert [(f.feature, f.line) for f in report.findings] == [], report.findings
    assert report.handling is Handling.RULE
    # Only the GREATEST outside the string is a risk, on its real line (after a multi-line literal too).
    w_line = source[:source.index("w := ")].count("\n") + 1
    assert [(r.function, r.line) for r in semantic_risks(source)] == [("GREATEST", w_line)]
