"""PR 1 v11: Oracle identifier resolution (Aarsh's PR #14 review, 19 Nov).

A reference resolves to an uploaded object only when Oracle would resolve it there:

1. Schema-qualified names: legacy.employees resolves; hr.employees and public.employees
   are other schemas' objects, so they stay unresolved (external), never the upload's table.
2. Quoted names: unquoted names are stored upper-case, quoted names exactly. "EMPLOYEES"
   matches EMPLOYEES; "Employees" does not.
3. The same rules for sequences (schema.seq.NEXTVAL) and package members (schema.pkg.member).
4. alias.column (e.salary) is not read as schema.object.
5. CREATE legacy.x defines x; a CREATE in another schema is refused as bad input.

Each case below failed on v10 (commit 0cf2b5e) unless it is marked as a guard: guards
pin behaviour v10 already had right, so the fix cannot break it.
"""

from __future__ import annotations

import pytest

from app.parsers.dependency_analyzer import analyze_dependencies, classify_reference
from app.parsers.oracle_parser import (
    DEFAULT_SCHEMA,
    ForeignSchemaDefinitionError,
    oracle_canonical,
    parse_sources,
    parse_sources_detailed,
)
from app.parsers.references import references_by_line

EMPLOYEES = "table:legacy.employees"
EMP_SEQ = "sequence:legacy.emp_seq"
PKG_F = "function:legacy.pkg.f"
PKG = "package:legacy.pkg"

UPLOAD = {
    "employees.sql": "CREATE TABLE employees (id NUMBER PRIMARY KEY, salary NUMBER);\n",
    "emp_seq.sql": "CREATE SEQUENCE emp_seq START WITH 1;\n",
    "pkg.sql": (
        "CREATE OR REPLACE PACKAGE pkg AS\n  FUNCTION f RETURN NUMBER;\nEND pkg;\n/\n"
        "CREATE OR REPLACE PACKAGE BODY pkg AS\n"
        "  FUNCTION f RETURN NUMBER IS\n  BEGIN\n    RETURN 1;\n  END f;\n"
        "END pkg;\n/\n"
    ),
}


def _proc(body: str, name: str = "p") -> str:
    return f"CREATE OR REPLACE PROCEDURE {name} IS\n  v NUMBER;\nBEGIN\n  {body}\nEND {name};\n/\n"


def _objects(body: str, extra: dict[str, str] | None = None):
    return parse_sources({**UPLOAD, **(extra or {}), "p.sql": _proc(body)})


def _deps(body: str, extra: dict[str, str] | None = None) -> set[str]:
    by_id = {o.object_id: o for o in _objects(body, extra)}
    return set(by_id["procedure:legacy.p"].depends_on)


def _kind(reference: str) -> str:
    return classify_reference(reference, {DEFAULT_SCHEMA})


def test_the_upload_schema_is_legacy_in_these_tests():
    # Guard: the cases below spell the upload schema as legacy.
    assert DEFAULT_SCHEMA == "legacy"


def test_oracle_canonical_names():
    assert oracle_canonical("employees") == "EMPLOYEES"
    assert oracle_canonical("Employees") == "EMPLOYEES"
    assert oracle_canonical('"EMPLOYEES"') == "EMPLOYEES"
    assert oracle_canonical('"Employees"') == "Employees"
    assert oracle_canonical('"employees"') == "employees"


# --- 1. schema-qualified tables and views ------------------------------------------------

UPLOAD_SCHEMA_SPELLINGS = ["legacy.employees", "LEGACY.EMPLOYEES", '"LEGACY"."EMPLOYEES"', 'Legacy . Employees']


@pytest.mark.parametrize("name", UPLOAD_SCHEMA_SPELLINGS)
def test_a_table_in_the_upload_schema_resolves(name):
    # Guard: v10 resolved these too.
    assert _deps(f"SELECT COUNT(*) INTO v FROM {name};") == {EMPLOYEES}


@pytest.mark.parametrize("schema", ["hr", "public", "HR"])
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT COUNT(*) INTO v FROM {t};",
        "SELECT COUNT(*) INTO v FROM dual JOIN {t} ON 1 = 1;",
        "UPDATE {t} SET salary = 0;",
        "INSERT INTO {t} (id) VALUES (1);",
        "DELETE FROM {t};",
        "MERGE INTO {t} t USING dual ON (1 = 0) WHEN NOT MATCHED THEN INSERT (id) VALUES (1);",
    ],
)
def test_a_table_in_another_schema_is_never_the_uploaded_table(schema, statement):
    deps = _deps(statement.format(t=f"{schema}.employees"))
    other = f"unresolved:{schema.lower()}.employees"
    assert EMPLOYEES not in deps
    assert other in deps
    assert _kind(other) == "external"


def test_reads_and_writes_still_list_names_as_before():
    # Guard: reads_tables / writes_tables keep their format (normalized, schema kept).
    by_id = {o.object_id: o for o in _objects('UPDATE hr.employees SET salary = 0; SELECT 1 INTO v FROM "Employees";')}
    p = by_id["procedure:legacy.p"]
    assert p.writes_tables == ["hr.employees"]
    assert p.reads_tables == ["employees"]


def test_another_schemas_table_blocks_nothing_and_asks_for_a_decision():
    objects = _objects("UPDATE hr.employees SET salary = 0;")
    graph = analyze_dependencies(objects)
    assert graph.blocked == {}
    assert graph.needs_decision == {"unresolved:hr.employees": ("procedure:legacy.p",)}
    assert not [link for link in graph.links if link.target == EMPLOYEES]


def test_a_trigger_on_another_schemas_table_is_not_attached_to_the_uploaded_table():
    trigger = (
        "CREATE OR REPLACE TRIGGER trg BEFORE UPDATE ON {t}\nFOR EACH ROW\n"
        "BEGIN\n  :NEW.salary := 0;\nEND;\n/\n"
    )
    other = {o.object_id: o for o in parse_sources({**UPLOAD, "trg.sql": trigger.format(t="hr.employees")})}
    assert other["trigger:legacy.trg"].trigger is None
    assert other["trigger:legacy.trg"].depends_on == ["unresolved:hr.employees"]
    own = {o.object_id: o for o in parse_sources({**UPLOAD, "trg.sql": trigger.format(t="legacy.employees")})}
    assert own["trigger:legacy.trg"].trigger.table == EMPLOYEES  # guard


def test_foreign_keys_follow_the_same_rules():
    tables = {
        **UPLOAD,
        "a.sql": "CREATE TABLE a (id NUMBER, e NUMBER REFERENCES legacy.employees (id));\n",
        "b.sql": "CREATE TABLE b (id NUMBER, d NUMBER REFERENCES hr.departments (id));\n",
        "c.sql": 'CREATE TABLE c (id NUMBER, CONSTRAINT c_fk FOREIGN KEY (id) REFERENCES "EMPLOYEES" (id));\n',
        "d.sql": "CREATE TABLE d (id NUMBER, e NUMBER REFERENCES hr.employees (id));\n",
        "f.sql": 'CREATE TABLE f (id NUMBER, e NUMBER REFERENCES "Employees" (id));\n',
    }
    by_id = {o.object_id: o for o in parse_sources(tables)}
    assert by_id["table:legacy.a"].depends_on == [EMPLOYEES]
    assert by_id["table:legacy.b"].depends_on == ["unresolved:hr.departments"]   # v10: unresolved:legacy.hr
    assert by_id["table:legacy.c"].depends_on == [EMPLOYEES]
    assert by_id["table:legacy.d"].depends_on == ["unresolved:hr.employees"]
    assert by_id["table:legacy.b"].reads_tables == ["hr.departments"]
    assert by_id["table:legacy.f"].depends_on == ["unresolved:legacy.employees"]


def test_references_inside_a_string_is_not_a_foreign_key():
    table = "CREATE TABLE notes (id NUMBER, txt VARCHAR2(40) DEFAULT 'REFERENCES hr.ghost (id)');\n"
    by_id = {o.object_id: o for o in parse_sources({**UPLOAD, "notes.sql": table})}
    assert by_id["table:legacy.notes"].depends_on == []
    assert by_id["table:legacy.notes"].reads_tables == []


# --- 2. quoted identifiers -----------------------------------------------------------------


def test_quoted_upper_case_matches_an_unquoted_name():
    assert _deps('SELECT COUNT(*) INTO v FROM "EMPLOYEES";') == {EMPLOYEES}   # guard


@pytest.mark.parametrize("name", ['"Employees"', '"employees"', 'legacy."Employees"'])
def test_quoted_mixed_or_lower_case_does_not_match_an_unquoted_name(name):
    deps = _deps(f"SELECT COUNT(*) INTO v FROM {name};")
    assert EMPLOYEES not in deps
    # Object ids are lower-case by contract, so the reference is kept as legacy.employees
    # and is missing (the upload's schema): the object is blocked, never silently linked.
    assert deps == {"unresolved:legacy.employees"}
    assert _kind("unresolved:legacy.employees") == "missing"


def test_a_quoted_schema_is_case_sensitive_too():
    deps = _deps('SELECT COUNT(*) INTO v FROM "legacy".employees;')
    assert EMPLOYEES not in deps


def test_a_table_created_with_a_quoted_name_needs_the_same_quoted_name():
    extra = {"mixed.sql": 'CREATE TABLE "Mixed" (id NUMBER);\n'}
    assert _deps('SELECT COUNT(*) INTO v FROM "Mixed";', extra) == {"table:legacy.mixed"}
    assert _deps("SELECT COUNT(*) INTO v FROM mixed;", extra) == {"unresolved:legacy.mixed"}
    assert _deps('SELECT COUNT(*) INTO v FROM "MIXED";', extra) == {"unresolved:legacy.mixed"}


def test_the_reason_explains_a_quoted_name_that_differs_only_in_case():
    objects = _objects('SELECT COUNT(*) INTO v FROM "Employees";')
    refs = references_by_line(objects, analyze_dependencies(objects))["procedure:legacy.p"]
    assert not refs.impact_complete
    assert refs.incomplete_reasons == (
        "refers to legacy.employees, which is not in the upload (missing); table:legacy.employees "
        "has the same lower-case name but does not match as written (quoted names are case-sensitive)",
    )


def test_lines_are_placed_on_the_object_the_parser_linked():
    # v10 placed hr.employees and "Employees" lines on table:legacy.employees too.
    body = (
        "SELECT COUNT(*) INTO v FROM employees;\n"
        "  UPDATE hr.employees SET salary = 0;\n"
        '  SELECT COUNT(*) INTO v FROM "Employees";\n'
        '  SELECT COUNT(*) INTO v FROM "EMPLOYEES";'
    )
    parsed = parse_sources_detailed({**UPLOAD, "p.sql": _proc(body)})
    graph = analyze_dependencies(parsed.objects)
    refs = references_by_line(parsed.objects, graph, parsed.locations, parsed.declared_names)["procedure:legacy.p"]
    placed = {(r.line, r.target, r.text) for r in refs.references}
    assert placed == {
        (4, EMPLOYEES, "employees"),
        (5, "unresolved:hr.employees", "hr.employees"),
        (6, "unresolved:legacy.employees", '"Employees"'),
        (7, EMPLOYEES, '"EMPLOYEES"'),
    }


def test_an_unqualified_call_inside_a_package_is_placed_on_that_packages_member():
    # Guard: placement now goes through the resolver; a same-package call must still land.
    body = (
        "CREATE OR REPLACE PACKAGE BODY pkg2 AS\n"
        "  FUNCTION g RETURN NUMBER IS\n  BEGIN\n    RETURN 2;\n  END g;\n"
        "  PROCEDURE q IS\n    v NUMBER;\n  BEGIN\n    v := g(1);\n  END q;\n"
        "END pkg2;\n/\n"
    )
    parsed = parse_sources_detailed({**UPLOAD, "pkg2.sql": body})
    graph = analyze_dependencies(parsed.objects)
    refs = references_by_line(parsed.objects, graph, parsed.locations, parsed.declared_names)
    assert {(r.line, r.target, r.kind) for r in refs["procedure:legacy.pkg2.q"].references} == {
        (4, "function:legacy.pkg2.g", "calls"),
    }


def test_a_name_with_too_many_parts_is_never_an_uploaded_object():
    from app.parsers.oracle_parser import NameResolver

    resolver = NameResolver(parse_sources(UPLOAD))
    assert resolver.table("legacy.employees") == EMPLOYEES
    assert resolver.table("legacy.employees.x") == "unresolved:legacy.employees.x"
    assert resolver.sequence(["legacy", "emp_seq", "x"]) == "unresolved:legacy.emp_seq.x"


def test_line_placement_knows_a_quoted_declaration():
    extra = {"mixed.sql": 'CREATE TABLE "Mixed" (id NUMBER);\n'}
    parsed = parse_sources_detailed({**UPLOAD, **extra, "p.sql": _proc('SELECT COUNT(*) INTO v FROM "Mixed";')})
    graph = analyze_dependencies(parsed.objects)
    refs = references_by_line(parsed.objects, graph, parsed.locations, parsed.declared_names)["procedure:legacy.p"]
    assert {(r.target, r.text) for r in refs.references} == {("table:legacy.mixed", '"Mixed"')}
    assert parsed.declared_names["table:legacy.mixed"] == "Mixed"
    assert parsed.declared_names[EMPLOYEES] == "EMPLOYEES"


# --- 3. sequences and package members ------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    ["emp_seq.NEXTVAL", "emp_seq.currval", "legacy.emp_seq.NEXTVAL", '"EMP_SEQ".NEXTVAL', '"LEGACY"."EMP_SEQ".NEXTVAL'],
)
def test_a_sequence_in_the_upload_schema_resolves(reference):
    # The first three are guards; v10 missed the quoted spellings.
    assert _deps(f"v := {reference};") == {EMP_SEQ}


@pytest.mark.parametrize(
    ("reference", "unresolved", "kind"),
    [
        ("hr.emp_seq.NEXTVAL", "unresolved:hr.emp_seq", "external"),
        ("public.emp_seq.NEXTVAL", "unresolved:public.emp_seq", "external"),
        ('"Emp_Seq".NEXTVAL', "unresolved:legacy.emp_seq", "missing"),
    ],
)
def test_a_sequence_elsewhere_or_with_a_different_quoted_case_does_not_resolve(reference, unresolved, kind):
    deps = _deps(f"v := {reference};")
    assert EMP_SEQ not in deps
    assert deps == {unresolved}
    assert _kind(unresolved) == kind


@pytest.mark.parametrize("call", ["pkg.f", "legacy.pkg.f", "LEGACY.PKG.F", '"PKG"."F"', '"LEGACY"."PKG"."F"', "legacy . pkg . f"])
def test_a_package_member_in_the_upload_schema_resolves(call):
    # pkg.f is a guard; v10 missed every schema-qualified or quoted spelling.
    assert _deps(f"v := {call};") == {PKG_F}


def test_another_schemas_package_member_is_not_the_uploaded_member():
    deps = _deps("v := hr.pkg.f;")
    assert PKG_F not in deps and PKG not in deps
    assert deps == {"unresolved:hr.pkg.f"}
    assert _kind("unresolved:hr.pkg.f") == "external"


def test_a_quoted_package_name_in_another_case_is_not_the_uploaded_package():
    # Guard ("pkg" could be an alias, so it is not reported either).
    deps = _deps('v := "pkg".f;')
    assert PKG_F not in deps and PKG not in deps


@pytest.mark.parametrize(
    ("call", "resolves"),
    [("g", True), ("G", True), ('"G"', True), ('"PKG2"."G"', True), ('"g"', False), ('"Pkg2".g', False)],
)
def test_calls_inside_the_package_follow_the_same_rules(call, resolves):
    # v10 missed "G"( and "PKG2"."G"( (quoted calls); the other four are guards.
    body = (
        "CREATE OR REPLACE PACKAGE BODY pkg2 AS\n"
        "  FUNCTION g RETURN NUMBER IS\n  BEGIN\n    RETURN 2;\n  END g;\n"
        "  PROCEDURE q IS\n    v NUMBER;\n  BEGIN\n    v := {call}(1);\n  END q;\n"
        "END pkg2;\n/\n"
    )
    by_id = {o.object_id: o for o in parse_sources({**UPLOAD, "pkg2.sql": body.format(call=call)})}
    assert ("function:legacy.pkg2.g" in by_id["procedure:legacy.pkg2.q"].depends_on) is resolves


def test_a_quoted_standalone_call_in_another_case_is_not_the_uploaded_routine():
    extra = {"calc.sql": "CREATE OR REPLACE FUNCTION calc RETURN NUMBER IS\nBEGIN\n  RETURN 1;\nEND calc;\n/\n"}
    assert "function:legacy.calc" in _deps('v := "CALC"(1);', extra)
    assert "function:legacy.calc" in _deps("v := legacy.calc(1);", extra)
    assert "function:legacy.calc" not in _deps('v := "Calc"(1);', extra)
    assert "function:legacy.calc" not in _deps("v := hr.calc(1);", extra)


def test_routines_declared_with_quoted_names_need_the_same_quoted_name():
    # v10 also dropped a package member declared with a quoted name ("Mx") from the IR.
    extra = {
        "calc2.sql": 'CREATE OR REPLACE FUNCTION "calc2" RETURN NUMBER IS\nBEGIN\n  RETURN 1;\nEND;\n/\n',
        "pk.sql": (
            'CREATE OR REPLACE PACKAGE BODY "Pk" AS\n'
            '  FUNCTION "Mx" RETURN NUMBER IS\n  BEGIN\n    RETURN 1;\n  END "Mx";\n'
            "  PROCEDURE q IS\n    v NUMBER;\n  BEGIN\n    v := {call};\n  END q;\n"
            'END "Pk";\n/\n'
        ),
    }

    def q_deps(call: str) -> list[str]:
        sources = {**UPLOAD, **extra, "pk.sql": extra["pk.sql"].format(call=call)}
        return {o.object_id: o for o in parse_sources(sources)}["procedure:legacy.pk.q"].depends_on

    assert "function:legacy.calc2" in _deps('v := "calc2"(1);', {"calc2.sql": extra["calc2.sql"]})
    assert "function:legacy.calc2" not in _deps("v := calc2(1);", {"calc2.sql": extra["calc2.sql"]})
    assert q_deps('"Mx"(1)') == ["function:legacy.pk.mx"]
    assert q_deps("mx(1)") == []
    assert q_deps('"Pk"."Mx"(1)') == ["function:legacy.pk.mx"]
    assert q_deps('pk."Mx"(1)') == []
    by_id = {o.object_id: o for o in parse_sources({**UPLOAD, **extra, "pk.sql": extra["pk.sql"].format(call="1")})}
    assert by_id["function:legacy.pk.mx"].package == "pk"


def test_hand_built_ir_without_source_references_still_resolves_its_listed_tables():
    # Guard: the IR may be built without source text; its listed tables still resolve.
    from app.contracts import IRObject, ObjectType
    from app.parsers.oracle_parser import resolve_object_dependencies

    table = IRObject(object_id=EMPLOYEES, name="employees", type=ObjectType.TABLE, source_code="")
    proc = IRObject(object_id="procedure:legacy.p", name="p", type=ObjectType.PROCEDURE,
                    reads_tables=["employees", "hr.jobs"], source_code="")
    resolve_object_dependencies([table, proc])
    assert proc.depends_on == [EMPLOYEES, "unresolved:hr.jobs"]


# --- 4. aliases are not schemas --------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT e.salary INTO v FROM employees e WHERE e.id = 1;",
        "SELECT e.salary INTO v FROM legacy.employees e WHERE e.id = 1;",
        'SELECT "E".salary INTO v FROM employees "E" WHERE "E".id = 1;',
        "UPDATE employees e SET e.salary = e.salary + 1 WHERE e.id = 1;",
    ],
)
def test_alias_dot_column_is_not_a_schema_reference(statement):
    # Guard (v10 got these right too): only the table is a dependency.
    assert _deps(statement) == {EMPLOYEES}


def test_record_fields_and_alias_columns_are_ignored():
    # Guard: neither part is the upload schema or an uploaded package, so nothing is linked
    # and nothing is reported as another schema's object.
    assert _deps("v := r.x.y;") == set()
    assert _deps("v := e.salary;") == set()


# --- 5. CREATE with a schema --------------------------------------------------------------------


def test_create_in_the_upload_schema_defines_the_object():
    sources = {
        "t.sql": "CREATE TABLE legacy.x (id NUMBER);\n",
        "u.sql": 'CREATE TABLE "LEGACY"."Y" (id NUMBER);\n',
        "p.sql": "CREATE OR REPLACE PROCEDURE LEGACY.p2 (p_id NUMBER) IS\nBEGIN\n  NULL;\nEND p2;\n/\n",
        "k.sql": (
            "CREATE OR REPLACE PACKAGE legacy.k AS\n  FUNCTION f RETURN NUMBER;\nEND k;\n/\n"
            "CREATE OR REPLACE PACKAGE BODY legacy.k AS\n"
            "  FUNCTION f RETURN NUMBER IS\n  BEGIN\n    RETURN 1;\n  END f;\nEND k;\n/\n"
        ),
        "s.sql": "CREATE SEQUENCE legacy.s;\n",
    }
    by_id = {o.object_id: o for o in parse_sources(sources)}
    assert sorted(by_id) == [
        "function:legacy.k.f", "package:legacy.k", "procedure:legacy.p2",
        "sequence:legacy.s", "table:legacy.x", "table:legacy.y",
    ]
    assert by_id["table:legacy.x"].name == "x"
    assert [p.name for p in by_id["procedure:legacy.p2"].params] == ["p_id"]


@pytest.mark.parametrize(
    ("statement", "schema", "object_name"),
    [
        ("CREATE TABLE hr.x (id NUMBER);", "hr", "x"),
        ("CREATE OR REPLACE VIEW public.v AS SELECT 1 AS a FROM dual;", "public", "v"),
        ('CREATE TABLE "legacy".x (id NUMBER);', '"legacy"', "x"),
        ("CREATE OR REPLACE PROCEDURE hr.p2 IS\nBEGIN\n  NULL;\nEND p2;\n/", "hr", "p2"),
    ],
)
def test_create_in_another_schema_is_refused_as_bad_input(statement, schema, object_name):
    with pytest.raises(ForeignSchemaDefinitionError) as info:
        parse_sources_detailed({**UPLOAD, "other.sql": "\n" + statement + "\n"})
    assert isinstance(info.value, ValueError)
    text = str(info.value)
    assert text.splitlines()[0] == "Objects in another schema: this upload is for schema legacy."
    assert f"  other.sql line 2: {schema}.{object_name}" in text
    assert text.splitlines()[-1] == "Upload each schema separately, or remove the schema prefix."


def test_every_object_in_another_schema_is_listed_sorted():
    sources = {"b.sql": "CREATE TABLE hr.b (id NUMBER);\n", "a.sql": "\n\nCREATE TABLE sales.a (id NUMBER);\n"}
    with pytest.raises(ForeignSchemaDefinitionError) as info:
        parse_sources_detailed(sources)
    lines = str(info.value).splitlines()
    assert lines[1:3] == ["  a.sql line 3: sales.a", "  b.sql line 1: hr.b"]


# --- found while fixing v11: trigger event words are not tables ----------------------------


@pytest.mark.parametrize("events", ["UPDATE", "UPDATE OR DELETE", "INSERT OR UPDATE", "DELETE OR UPDATE OR INSERT"])
def test_a_triggers_event_words_are_not_tables(events):
    # v10 read BEFORE UPDATE ON t as a write to a table "on" (UPDATE OR DELETE: "or"), so
    # every such trigger was blocked by a missing legacy.on / legacy.or.
    trigger = (
        f"CREATE OR REPLACE TRIGGER trg BEFORE {events} ON employees\nFOR EACH ROW\n"
        "BEGIN\n  :NEW.salary := 0;\nEND;\n/\n"
    )
    objects = parse_sources({**UPLOAD, "trg.sql": trigger})
    trg = {o.object_id: o for o in objects}["trigger:legacy.trg"]
    assert trg.writes_tables == []
    assert trg.depends_on == [EMPLOYEES]
    assert analyze_dependencies(objects).blocked == {}
