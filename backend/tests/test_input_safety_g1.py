"""PR 1 / G1: an object defined more than once is refused, naming every file and line.

Also pins where objects start (file and line), which the readiness report and repair
tickets rely on, and today's behaviour for package overloads (refused loudly, before G1).
"""

from __future__ import annotations

import pytest

from app.contracts.object_ids import ObjectIdCollisionError
from app.parsers.oracle_parser import (
    DuplicateDefinitionError,
    SourceLocation,
    parse_sources,
    parse_sources_detailed,
    remove_comments,
    split_statements,
    split_statements_with_lines,
)
from tests.pr1_samples import oracle_sample_sources

F1 = "CREATE OR REPLACE FUNCTION calc_bonus RETURN NUMBER IS\nBEGIN\n  RETURN 1;\nEND calc_bonus;\n/\n"
F2 = "CREATE OR REPLACE FUNCTION calc_bonus RETURN NUMBER IS\nBEGIN\n  RETURN 2;\nEND calc_bonus;\n/\n"
TABLE = "CREATE TABLE employees (emp_id NUMBER PRIMARY KEY);\n"

SPEC = "CREATE OR REPLACE PACKAGE pay_pkg AS\n  FUNCTION net(p NUMBER) RETURN NUMBER;\nEND pay_pkg;\n/\n"
BODY = (
    "CREATE OR REPLACE PACKAGE BODY pay_pkg AS\n"
    "  FUNCTION net(p NUMBER) RETURN NUMBER IS\n"
    "  BEGIN\n"
    "    RETURN p * 0.8;\n"
    "  END net;\n"
    "END pay_pkg;\n"
    "/\n"
)


def _error(sources: dict[str, str]) -> DuplicateDefinitionError:
    with pytest.raises(DuplicateDefinitionError) as info:
        parse_sources(sources)
    return info.value


def test_same_object_in_two_files_is_refused_naming_both():
    err = _error({"04_functions.sql": F1, "backup/04_old.sql": "\n\n" + F2})
    (dup,) = err.duplicates
    assert dup.object_id == "function:legacy.calc_bonus"
    assert dup.places == (SourceLocation("04_functions.sql", 1), SourceLocation("backup/04_old.sql", 3))
    assert dup.identical is False
    assert "04_functions.sql line 1; backup/04_old.sql line 3" in str(err)
    assert "(identical)" not in str(err)


def test_error_text_is_the_same_whatever_the_upload_order():
    forward = _error({"a.sql": F1, "b.sql": F2})
    backward = _error({"b.sql": F2, "a.sql": F1})
    assert str(forward) == str(backward)
    assert forward.duplicates == backward.duplicates


def test_identical_copies_are_refused_and_marked_identical():
    err = _error({"a.sql": F1, "copy.sql": F1.replace("  RETURN 1;", "RETURN    1;")})
    assert err.duplicates[0].identical is True
    assert "(identical)" in str(err)


def test_same_file_defining_an_object_twice_is_refused():
    err = _error({"a.sql": F1 + "\n" + F2})
    assert err.duplicates[0].places == (SourceLocation("a.sql", 1), SourceLocation("a.sql", 7))


def test_tables_are_checked_too():
    err = _error({"01_schema.sql": TABLE, "02_more.sql": TABLE})
    assert err.duplicates[0].object_id == "table:legacy.employees"


def test_every_duplicate_is_listed_in_one_error_sorted():
    err = _error({"z.sql": F1 + TABLE, "a.sql": TABLE + F2})
    assert [d.object_id for d in err.duplicates] == ["function:legacy.calc_bonus", "table:legacy.employees"]
    text = str(err)
    assert text.startswith("Duplicate definitions: each object must be defined once.")
    assert text.endswith("Remove the extra definitions and run again.")
    assert text.index("calc_bonus") < text.index("employees")


def test_package_spec_plus_body_is_not_a_duplicate():
    result = parse_sources_detailed({"pkg_spec.sql": SPEC, "pkg_body.sql": BODY})
    ids = {o.object_id for o in result.objects}
    assert {"package:legacy.pay_pkg", "function:legacy.pay_pkg.net"} <= ids


def test_two_package_bodies_are_refused():
    err = _error({"a.sql": SPEC + BODY, "b.sql": BODY})
    assert err.duplicates[0].object_id == "package:legacy.pay_pkg"
    assert err.duplicates[0].identical is True


def test_two_package_specs_are_refused():
    err = _error({"a.sql": SPEC, "b.sql": SPEC + BODY})
    assert [p.file for p in err.duplicates[0].places] == ["a.sql", "b.sql"]


def test_duplicates_are_a_value_error_so_callers_treat_them_as_bad_input():
    assert issubclass(DuplicateDefinitionError, ValueError)


def test_overloads_are_refused_before_g1_today():
    # Oracle allows overloads; our object ids carry no parameter types, so both get the same
    # id and the parser stops loudly. Supporting overloads is a contract change (follow-up).
    overloaded = (
        "CREATE OR REPLACE PACKAGE BODY emp_pkg AS\n"
        "  PROCEDURE calc(p_id NUMBER) IS\n  BEGIN\n    NULL;\n  END calc;\n"
        "  PROCEDURE calc(p_id NUMBER, p_grade VARCHAR2) IS\n  BEGIN\n    NULL;\n  END calc;\n"
        "END emp_pkg;\n/\n"
    )
    with pytest.raises(ObjectIdCollisionError) as info:
        parse_sources({"emp_pkg.sql": overloaded})
    assert not isinstance(info.value, DuplicateDefinitionError)
    assert "procedure:legacy.emp_pkg.calc" in str(info.value)


# --- where objects start ----------------------------------------------------------------


def test_block_comments_keep_their_line_breaks():
    sql = "/* one\n two\n three */\nCREATE TABLE t (x NUMBER);\n"
    cleaned = remove_comments(sql)
    assert cleaned.count("\n") == sql.count("\n")
    assert "two" not in cleaned


def test_statements_carry_their_first_line():
    sql = "\n\nCREATE TABLE a (x NUMBER);\n\n-- note\nCREATE TABLE b (\n  y NUMBER\n);\n" + F1
    pieces = split_statements_with_lines(remove_comments(sql))
    assert [line for _, line in pieces] == [3, 6, 9]
    assert split_statements(remove_comments(sql)) == [s for s, _ in pieces]


def test_locations_point_at_the_create_line_after_comments():
    sql = "-- header\n/* a long\n   block comment\n*/\n\n" + F1 + "\n" + TABLE
    result = parse_sources_detailed({"x.sql": sql})
    assert result.locations["function:legacy.calc_bonus"] == SourceLocation("x.sql", 6)
    assert result.locations["table:legacy.employees"] == SourceLocation("x.sql", 12)


def test_package_member_location_is_its_line_in_the_body_file():
    result = parse_sources_detailed({"pkg_spec.sql": SPEC, "pkg_body.sql": "-- body\n" + BODY})
    assert result.locations["package:legacy.pay_pkg"] == SourceLocation("pkg_spec.sql", 1)
    assert result.locations["function:legacy.pay_pkg.net"] == SourceLocation("pkg_body.sql", 3)


def test_parse_sources_returns_the_same_objects_as_the_detailed_form():
    sources = {"pkg_spec.sql": SPEC, "pkg_body.sql": BODY, "t.sql": TABLE}
    assert parse_sources(sources) == parse_sources_detailed(sources).objects


def test_every_sample_object_has_a_location_on_its_create_line():
    sources = oracle_sample_sources()
    result = parse_sources_detailed(sources)
    assert set(result.locations) == {o.object_id for o in result.objects}
    for object_id, place in result.locations.items():
        line = sources[place.file].splitlines()[place.line - 1].upper()
        name = object_id.rsplit(".", 1)[-1].upper()
        assert name in line, (object_id, place, line)


def test_location_is_the_create_line_when_the_statement_starts_earlier():
    # SQL*Plus lines such as PROMPT have no semicolon, so they start the same statement.
    sql = "PROMPT creating the table\nCREATE TABLE employees (emp_id NUMBER);\n"
    result = parse_sources_detailed({"x.sql": sql})
    assert result.locations["table:legacy.employees"] == SourceLocation("x.sql", 2)


def test_places_are_sorted_by_file_then_line():
    err = _error({"b.sql": F1, "a.sql": "\n\n\n\n" + F2})
    assert err.duplicates[0].places == (SourceLocation("a.sql", 5), SourceLocation("b.sql", 1))
