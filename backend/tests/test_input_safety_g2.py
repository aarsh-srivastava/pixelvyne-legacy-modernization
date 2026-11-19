"""PR 1 / G2 parser half and G10 groundwork.

Unresolved references are listed up front and sorted (missing / system / external), with
every object they block; links are typed; triggers are linked to the tables that fire them
without changing the execution order; and each reference is placed on its line.
"""

from __future__ import annotations

import pytest

from app.parsers.dependency_analyzer import Link, analyze_dependencies, classify_reference
from app.parsers.oracle_parser import parse_sources, parse_sources_detailed
from app.parsers.references import references_by_line
from tests.pr1_samples import oracle_sample_sources

APPLY_RAISE = "procedure:legacy.apply_raise"
EMPLOYEES = "table:legacy.employees"
TRIGGER = "trigger:legacy.emp_salary_audit_trg"


@pytest.fixture(scope="module")
def sample():
    sources = oracle_sample_sources()
    parsed = parse_sources_detailed(sources)
    return sources, parsed, analyze_dependencies(parsed.objects)


def _proc(name: str, body: str) -> str:
    return f"CREATE OR REPLACE PROCEDURE {name} IS\n  v NUMBER;\nBEGIN\n{body}\nEND {name};\n/\n"


# --- typed links and trigger links ---------------------------------------------------------


def test_links_say_how_each_object_is_used(sample):
    _, _, graph = sample
    links = set(graph.links)
    assert Link(APPLY_RAISE, EMPLOYEES, "writes") in links
    assert Link(APPLY_RAISE, EMPLOYEES, "reads") in links
    assert Link("procedure:legacy.payroll_pkg.close_month", "function:legacy.payroll_pkg.net_pay", "calls") in links
    assert Link("procedure:legacy.monthly_payroll", "sequence:legacy.payroll_run_seq", "references") in links
    assert Link(TRIGGER, EMPLOYEES, "references") in links
    assert {link.kind for link in graph.links} <= {"calls", "reads", "writes", "references"}


def test_every_edge_has_at_least_one_link_and_no_link_lacks_an_edge(sample):
    _, _, graph = sample
    edges = {(e.dependent, e.dependency) for e in graph.edges}
    assert {(link.source, link.target) for link in graph.links} == edges


def test_a_table_fires_its_trigger(sample):
    _, _, graph = sample
    assert graph.fires == (Link(EMPLOYEES, TRIGGER, "fires"),)


def test_fires_links_never_enter_the_execution_order_or_cycles(sample):
    _, _, graph = sample
    assert (EMPLOYEES, TRIGGER) not in {(e.dependent, e.dependency) for e in graph.edges}
    assert graph.cycles == ()
    order = graph.execution_order
    assert order.index(EMPLOYEES) < order.index(TRIGGER)


def test_upstream_counts_distance_from_the_object(sample):
    _, _, graph = sample
    up = graph.upstream(APPLY_RAISE)
    assert up[EMPLOYEES] == 1
    assert up[TRIGGER] == 2
    assert up["table:legacy.departments"] == 2
    assert APPLY_RAISE not in up


def test_downstream_of_a_trigger_is_its_table_and_the_table_writers_only(sample):
    _, _, graph = sample
    down = graph.downstream(TRIGGER)
    assert down == {EMPLOYEES: 1, APPLY_RAISE: 2}
    assert "function:legacy.get_emp_grade" not in down  # reads employees, never fires the trigger


def test_downstream_follows_calls_backwards(sample):
    _, _, graph = sample
    assert graph.downstream("function:legacy.payroll_pkg.net_pay") == {
        "procedure:legacy.payroll_pkg.close_month": 1
    }
    assert graph.downstream("table:legacy.departments")[EMPLOYEES] == 1


def test_the_sample_upload_has_nothing_unresolved_or_blocked(sample):
    _, _, graph = sample
    assert graph.unresolved == ()
    assert graph.blocked == {}
    assert graph.needs_decision == {}


# --- unresolved references: missing, system, external --------------------------------------


@pytest.mark.parametrize(
    ("reference", "kind"),
    [
        ("unresolved:legacy.dual", "system"),
        ("unresolved:sys.all_objects", "system"),
        ("unresolved:public.some_synonym", "external"),  # PUBLIC is not a system schema (review, 18 Nov)
        ("unresolved:legacy.user_tables", "system"),
        ("unresolved:legacy.v$session", "system"),
        ("unresolved:dbms_lock.sleep", "system"),
        ("unresolved:legacy.employees", "missing"),
        ("unresolved:legacy.user_accounts", "missing"),  # looks like a dictionary view, is not one
        ("unresolved:ext_schema.employee_records", "external"),
        ("unresolved:hr.pkg.fn", "external"),
    ],
)
def test_unresolved_references_are_sorted_into_three_kinds(reference, kind):
    assert classify_reference(reference, {"legacy"}) == kind


def test_classify_refuses_a_resolved_id():
    with pytest.raises(ValueError):
        classify_reference(EMPLOYEES, {"legacy"})


def test_dual_and_system_views_never_block_anything():
    body = "  SELECT 1 INTO v FROM dual;\n  SELECT COUNT(*) INTO v FROM sys.all_objects;"
    objects = parse_sources({"p.sql": _proc("p", body)})
    graph = analyze_dependencies(objects)
    assert {u.kind for u in graph.unresolved} == {"system"}
    assert graph.blocked == {}
    assert graph.needs_decision == {}


def test_a_missing_table_blocks_its_users_transitively():
    sources = {
        "p.sql": _proc("p", "  SELECT COUNT(*) INTO v FROM employees;"),
        "q.sql": _proc("q", "  p;"),
        "r.sql": _proc("r", "  q();"),
        "s.sql": _proc("s", "  NULL;"),
    }
    graph = analyze_dependencies(parse_sources(sources))
    (missing,) = graph.unresolved
    assert (missing.reference, missing.kind, missing.referenced_by) == (
        "unresolved:legacy.employees", "missing", ("procedure:legacy.p",)
    )
    assert graph.blocked == {
        "unresolved:legacy.employees": ("procedure:legacy.p", "procedure:legacy.q", "procedure:legacy.r")
    }


def test_another_schema_needs_a_decision_and_does_not_block():
    sources = {
        "p.sql": _proc("p", "  SELECT COUNT(*) INTO v FROM ext_schema.employee_records;"),
        "q.sql": _proc("q", "  p;"),
    }
    graph = analyze_dependencies(parse_sources(sources))
    assert graph.blocked == {}
    assert graph.needs_decision == {
        "unresolved:ext_schema.employee_records": ("procedure:legacy.p", "procedure:legacy.q")
    }


def test_a_missing_reference_inside_a_cycle_blocks_the_whole_cycle():
    sources = {
        "a.sql": _proc("a", "  b;\n  SELECT COUNT(*) INTO v FROM ghost_table;"),
        "b.sql": _proc("b", "  a;"),
    }
    graph = analyze_dependencies(parse_sources(sources))
    assert graph.cycles == (("procedure:legacy.a", "procedure:legacy.b"),)
    assert graph.blocked == {"unresolved:legacy.ghost_table": ("procedure:legacy.a", "procedure:legacy.b")}


# --- calls to standalone routines (were not linked before PR 1) ----------------------------


def test_calls_to_standalone_routines_become_dependencies():
    sources = {
        "f.sql": (
            "CREATE OR REPLACE FUNCTION bonus_of(p NUMBER) RETURN NUMBER IS\n"
            "BEGIN\n  RETURN p;\nEND bonus_of;\n/\n"
        ),
        "p.sql": _proc("log_run", "  NULL;"),
        "q.sql": _proc("q", "  v := bonus_of(1);\n  log_run;"),
    }
    by_id = {o.object_id: o for o in parse_sources(sources)}
    assert by_id["procedure:legacy.q"].depends_on == ["function:legacy.bonus_of", "procedure:legacy.log_run"]
    assert by_id["function:legacy.bonus_of"].depends_on == []  # its own name is not a call


def test_a_routine_name_inside_a_string_is_not_a_call():
    sources = {
        "p.sql": _proc("log_run", "  NULL;"),
        "q.sql": _proc("q", "  DBMS_OUTPUT.PUT_LINE('log_run;');"),
    }
    by_id = {o.object_id: o for o in parse_sources(sources)}
    assert "procedure:legacy.log_run" not in by_id["procedure:legacy.q"].depends_on



# --- q-quoted strings (Aarsh, 19 Nov): the scanner uses the same masker as detection ------

_QUOTED_SOURCES = {
    "t.sql": "CREATE TABLE staging (id NUMBER);\n/\nCREATE SEQUENCE audit_seq;\n/\n",
    "pkg.sql": (
        "CREATE OR REPLACE PACKAGE util IS\n  PROCEDURE flush;\nEND util;\n/\n"
        "CREATE OR REPLACE PACKAGE BODY util IS\n  PROCEDURE flush IS BEGIN NULL; END flush;\nEND util;\n/\n"
    ),
    "p.sql": _proc("log_run", "  NULL;"),
}


@pytest.mark.parametrize("literal", [
    "q'[it's FROM staging]'",                      # an apostrophe ends an ordinary-literal scan early
    "Q'{don't INSERT INTO staging}'",
    "q'<it's: log_run; util.flush(1)>'",
    "q'!it's audit_seq.NEXTVAL!'",
    "nq'[it's FROM staging; log_run;]'",          # national q-quote
    "q'[x]' || 'it''s FROM staging'",              # a q-quote next to an ordinary literal
])
def test_names_inside_q_quoted_strings_are_not_dependencies(literal):
    sources = {**_QUOTED_SOURCES, "q.sql": _proc("q", f"  DBMS_OUTPUT.PUT_LINE({literal});")}
    by_id = {o.object_id: o for o in parse_sources(sources)}
    q = by_id["procedure:legacy.q"]
    assert q.reads_tables == [] and q.writes_tables == []
    assert q.depends_on == [], q.depends_on


def test_the_same_names_outside_strings_are_still_dependencies():
    body = "  INSERT INTO staging VALUES (audit_seq.NEXTVAL);\n  util.flush;\n  log_run;"
    sources = {**_QUOTED_SOURCES, "q.sql": _proc("q", f"  v := q'[it's]';\n{body}")}
    by_id = {o.object_id: o for o in parse_sources(sources)}
    assert by_id["procedure:legacy.q"].depends_on == [
        "package:legacy.util", "procedure:legacy.log_run", "sequence:legacy.audit_seq", "table:legacy.staging",
    ]



_PACKAGE = """CREATE OR REPLACE PACKAGE util IS
  PROCEDURE flush;
  PROCEDURE helper;
END util;
/
CREATE OR REPLACE PACKAGE BODY util IS
  PROCEDURE flush IS
  BEGIN
    NULL;
  END flush;

  PROCEDURE helper IS
  BEGIN
    {body}
  END helper;
END util;
/
"""


@pytest.mark.parametrize("body, depends_on", [
    ("DBMS_OUTPUT.PUT_LINE(q'[it's flush(1)]');", []),
    ("flush(1);", ["procedure:legacy.util.flush"]),
    ("flush;", ["procedure:legacy.util.flush"]),          # no arguments: still a call (as in 4b)
    ("other_pkg.flush(1);", []),   # another package's flush, not this one (detection flags it)
])
def test_same_package_calls_and_q_quoted_strings(body, depends_on):
    by_id = {o.object_id: o for o in parse_sources({"pkg.sql": _PACKAGE.format(body=body)})}
    assert by_id["procedure:legacy.util.helper"].depends_on == depends_on



# Malformed quoting (Aarsh, 19 Nov): _blank_strings falls back to the old regex when the
# masker cannot read the source. That fallback only shapes dependencies; such an object must
# still never reach a deploy, on either route, and is never retried.
@pytest.mark.parametrize("literal", ["q'[it's FROM staging; END q;", "'never closed; END q;", "nq'{open"])
def test_malformed_quoting_never_reaches_a_deploy(literal):
    from app.migration.converter import OracleToPostgresConverter
    from app.migration.llm_fallback import GeminiFallback
    from app.migration.postgres_deployer import DeployResult
    from app.orchestration.repair_loop import can_retry

    deploys, prompts = [], []

    class Deployer:
        def deploy(self, code, target):
            deploys.append(code)
            return DeployResult(ok=True)

    def generate(prompt, *, system=None):
        prompts.append(prompt)
        raise AssertionError("Gemini must not be asked")

    q_source = f"CREATE OR REPLACE PROCEDURE q IS v VARCHAR2(50);\nBEGIN\n  v := {literal}\n/\n"
    sources = {**_QUOTED_SOURCES, "q.sql": q_source}
    objects = parse_sources(sources)
    q = next(o for o in objects if o.object_id == "procedure:legacy.q")
    for llm in (None, GeminiFallback(generate)):
        result = OracleToPostgresConverter(Deployer(), objects, llm=llm).convert(q)
        assert not result.compiled_ok and result.needs_review and result.failure_code == "review_required"
        assert "source could not be read" in result.compile_error
        assert not can_retry(result)
    assert deploys == [] and prompts == []


def test_an_unreadable_object_does_not_stop_the_package_analysis():
    from app.migration.features import analyze_package

    package = _QUOTED_SOURCES["pkg.sql"].replace(
        "IS\n  PROCEDURE flush;", "IS\n  counter NUMBER := 0;\n  PROCEDURE flush;", 1
    )
    (pkg,) = [o for o in parse_sources({"pkg.sql": package}) if o.type.value == "package"]
    assigns = "BEGIN util.counter := 1; v := q'[open"
    assert analyze_package(pkg.source_code, [assigns]).variables["counter"].assigned  # never missed
    mentions = "BEGIN v := 'util.counter := 1"
    assert analyze_package(pkg.source_code, [mentions]).variables["counter"].assigned  # safe direction
    assert not analyze_package(pkg.source_code, ["BEGIN v := 1; END;"]).variables["counter"].assigned


# --- references by line ------------------------------------------------------------------


def test_the_update_line_in_apply_raise_writes_employees_and_fires_the_trigger(sample):
    sources, parsed, graph = sample
    refs = references_by_line(parsed.objects, graph, parsed.locations)[APPLY_RAISE]
    (update_line,) = {r.line for r in refs.references if r.kind == "writes"}
    on_line = refs.on_line(update_line)
    assert {(r.target, r.kind) for r in on_line} == {(EMPLOYEES, "writes"), (TRIGGER, "fires")}
    file_line = on_line[0].file_line
    assert "UPDATE EMPLOYEES" in sources["05_procedures.sql"].splitlines()[file_line - 1].upper()
    assert refs.impact_complete is True


def test_every_sample_reference_points_at_a_line_naming_its_target(sample):
    sources, parsed, graph = sample
    by_id = {o.object_id: o for o in parsed.objects}
    for object_id, refs in references_by_line(parsed.objects, graph, parsed.locations).items():
        obj = by_id[object_id]
        for ref in refs.references:
            assert ref.text.lower() in obj.source_code.splitlines()[ref.line - 1].lower()
            if ref.file_line is not None:
                assert ref.text.lower() in sources[obj.source_file].splitlines()[ref.file_line - 1].lower()


def test_without_locations_file_lines_are_unknown(sample):
    _, parsed, graph = sample
    refs = references_by_line(parsed.objects, graph)[APPLY_RAISE]
    assert refs.references and all(r.file_line is None for r in refs.references)


def test_unresolved_references_are_on_their_lines_and_make_the_analysis_incomplete():
    source = _proc("p", "  SELECT COUNT(*) INTO v FROM ext_schema.employee_records;\n  SELECT 1 INTO v FROM dual;")
    parsed = parse_sources_detailed({"p.sql": source})
    refs = references_by_line(parsed.objects, analyze_dependencies(parsed.objects), parsed.locations)[
        "procedure:legacy.p"
    ]
    kinds = {(r.line, r.kind) for r in refs.references}
    assert (4, "external") in kinds and (5, "system") in kinds
    assert refs.impact_complete is False
    assert refs.incomplete_reasons == (
        "refers to ext_schema.employee_records, which is not in the upload (external)",
    )


def test_dynamic_sql_makes_the_analysis_incomplete():
    parsed = parse_sources_detailed({"p.sql": _proc("p", "  EXECUTE IMMEDIATE 'DELETE FROM x';")})
    refs = references_by_line(parsed.objects, analyze_dependencies(parsed.objects))["procedure:legacy.p"]
    assert refs.impact_complete is False
    assert refs.incomplete_reasons == ("dynamic SQL: what it runs is only known at run time",)


def test_names_in_comments_and_strings_are_not_references():
    sources = {
        "t.sql": "CREATE TABLE employees (emp_id NUMBER);\n",
        "p.sql": _proc("p", (
            "  -- employees are updated elsewhere\n"
            "  DBMS_OUTPUT.PUT_LINE('employees');\n"
            "  SELECT COUNT(*) INTO v FROM employees;"
        )),
    }
    parsed = parse_sources_detailed(sources)
    refs = references_by_line(parsed.objects, analyze_dependencies(parsed.objects))["procedure:legacy.p"]
    assert [(r.line, r.kind) for r in refs.references] == [(6, "reads")]


def test_words_inside_strings_are_not_table_references():
    source = _proc("p", (
        "  DBMS_OUTPUT.PUT_LINE('Loaded from staging into archive');\n"
        "  EXECUTE IMMEDIATE 'DELETE FROM x';"
    ))
    (obj,) = parse_sources({"p.sql": source})
    assert obj.reads_tables == [] and obj.writes_tables == []
    assert obj.depends_on == []


def test_file_lines_count_from_the_create_line_when_a_prompt_comes_first():
    sources = {
        "dept.sql": "CREATE TABLE departments (dept_id NUMBER PRIMARY KEY);\n",
        "emp.sql": (
            "PROMPT employees\nCREATE TABLE employees (\n"
            "  dept_id NUMBER REFERENCES departments (dept_id)\n);\n"
        ),
    }
    parsed = parse_sources_detailed(sources)
    refs = references_by_line(parsed.objects, analyze_dependencies(parsed.objects), parsed.locations)
    (ref,) = refs["table:legacy.employees"].references
    assert (ref.target, ref.file_line) == ("table:legacy.departments", 3)


# --- review fix 2 (Aarsh, 18 Nov): PUBLIC is not a system schema ------------------------


def test_reported_case_public_is_external_when_the_upload_does_not_use_it():
    assert classify_reference("unresolved:public.some_object", {"app"}) == "external"


def test_public_is_missing_when_the_upload_uses_it():
    assert classify_reference("unresolved:public.some_object", {"public"}) == "missing"


def test_sys_and_system_stay_system_schemas():
    assert classify_reference("unresolved:sys.all_objects", {"app"}) == "system"
    assert classify_reference("unresolved:system.some_table", {"app"}) == "system"


def test_a_public_reference_needs_a_decision_and_is_not_dropped():
    sources = {"p.sql": _proc("p", "  SELECT COUNT(*) INTO v FROM public.shared_rates;")}
    graph = analyze_dependencies(parse_sources(sources))
    assert [(u.reference, u.kind) for u in graph.unresolved] == [("unresolved:public.shared_rates", "external")]
    assert graph.needs_decision == {"unresolved:public.shared_rates": ("procedure:legacy.p",)}
