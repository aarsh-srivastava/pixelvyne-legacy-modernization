"""Canonical object_id contract (0.2). No database needed."""

import pytest
from pydantic import ValidationError

from app.contracts import (
    CONTRACT_VERSION,
    OBJECT_ID_PATTERN,
    ObjectIdCollisionError,
    ObjectRef,
    ObjectType,
    TriggerInfo,
    check_unique_object_ids,
    default_schema,
    is_object_id,
    make_object_id,
    make_unresolved_id,
    normalize_identifier,
    normalize_signature,
    parse_object_id,
)
from app.contracts.object_ids import OBJECT_KINDS


def test_contract_version_is_bumped():
    assert CONTRACT_VERSION == "0.2"


def test_kinds_stay_in_step_with_object_type():
    assert set(OBJECT_KINDS) == {t.value for t in ObjectType}


# ---------------------------------------------------------------- normalization

@pytest.mark.parametrize("raw", ["EMPLOYEES", "employees", '"EMPLOYEES"', '"Employees"', "  Employees "])
def test_identifiers_normalize_to_lower_case(raw):
    assert normalize_identifier(raw) == "employees"


@pytest.mark.parametrize("raw", ["", '""', "my table", "a.b", "1abc", "emp@link", '"My Table"'])
def test_unsupported_identifiers_are_rejected(raw):
    with pytest.raises(ValueError):
        normalize_identifier(raw)


def test_oracle_identifier_symbols_are_kept():
    assert normalize_identifier("PAY$RUN#2") == "pay$run#2"


@pytest.mark.parametrize("user, expected", [("LEGACY", "legacy"), ("hr", "hr"), (None, "legacy"),
                                            ("", "legacy"), ("  ", "legacy")])
def test_default_schema_comes_from_the_caller(user, expected):
    assert default_schema(user) == expected


def test_signature_types_drop_size_and_keep_order():
    assert normalize_signature(["VARCHAR2(20)", "NUMBER(10, 2)", "DATE"]) == ("varchar2", "number", "date")
    assert normalize_signature(["TIMESTAMP WITH TIME ZONE"]) == ("timestamp_with_time_zone",)
    assert normalize_signature(["employees.base_salary%TYPE"]) == ("employees.base_salary%type",)


# ---------------------------------------------------------------- make + parse

@pytest.mark.parametrize("kwargs, expected", [
    (dict(kind="table", name="EMPLOYEES", schema="LEGACY"), "table:legacy.employees"),
    (dict(kind=ObjectType.PROCEDURE, name="calc_bonus", schema="legacy"), "procedure:legacy.calc_bonus"),
    (dict(kind="package", name="payroll_pkg", schema="legacy"), "package:legacy.payroll_pkg"),
    (dict(kind="function", name="NET_PAY", schema="legacy", package="PAYROLL_PKG"),
     "function:legacy.payroll_pkg.net_pay"),
    (dict(kind="trigger", name='"Emp_Salary_Audit_Trg"', schema="legacy"), "trigger:legacy.emp_salary_audit_trg"),
])
def test_make_object_id(kwargs, expected):
    assert make_object_id(**kwargs) == expected
    assert OBJECT_ID_PATTERN.match(expected)


@pytest.mark.parametrize("object_id", [
    "table:legacy.employees",
    "view:hr.org_chart",
    "sequence:legacy.salary_audit_seq",
    "package:legacy.payroll_pkg",
    "function:legacy.payroll_pkg.net_pay",
    "procedure:legacy.payroll_pkg.close_month",
    "procedure:hr.pkg.calculate",
    "procedure:hr.pkg.calculate()",
    "procedure:hr.pkg.calculate(number)",
    "procedure:hr.pkg.calculate(number,date)",
    "function:hr.fmt(employees.base_salary%type)",
])
def test_round_trip(object_id):
    ref = parse_object_id(object_id)
    rebuilt = make_object_id(ref.kind, ref.name, schema=ref.schema, package=ref.package,
                             signature=list(ref.signature) if ref.signature is not None else None)
    assert rebuilt == object_id


def test_parse_splits_three_part_ids_into_schema_package_member():
    assert parse_object_id("function:legacy.payroll_pkg.net_pay") == ObjectRef(
        "function", "legacy", "payroll_pkg", "net_pay", None)
    assert parse_object_id("table:hr.employees") == ObjectRef("table", "hr", None, "employees", None)


def test_signature_only_when_the_caller_passes_one():
    # The helper never guesses overloads: no signature argument, no suffix.
    assert make_object_id("procedure", "calculate", schema="hr", package="pkg") == "procedure:hr.pkg.calculate"
    overloads = {
        make_object_id("procedure", "calculate", schema="hr", package="pkg", signature=[]),
        make_object_id("procedure", "calculate", schema="hr", package="pkg", signature=["NUMBER"]),
        make_object_id("procedure", "calculate", schema="hr", package="pkg", signature=["NUMBER(10)", "DATE"]),
    }
    assert overloads == {"procedure:hr.pkg.calculate()", "procedure:hr.pkg.calculate(number)",
                         "procedure:hr.pkg.calculate(number,date)"}
    assert parse_object_id("procedure:hr.pkg.calculate()").signature == ()
    assert parse_object_id("procedure:hr.pkg.calculate").signature is None


@pytest.mark.parametrize("kwargs", [
    dict(kind="table", name="employees", schema="legacy", package="pkg"),     # tables are not members
    dict(kind="table", name="employees", schema="legacy", signature=[]),      # nor overloaded
    dict(kind="synonym", name="emp", schema="legacy"),                        # unknown kind
    dict(kind="unresolved", name="emp", schema="legacy"),                     # use make_unresolved_id
    dict(kind="table", name="employees", schema=""),                          # schema required
    dict(kind="table", name="hr.employees", schema="legacy"),                 # name must be one part
])
def test_make_object_id_rejects_invalid_input(kwargs):
    with pytest.raises(ValueError):
        make_object_id(**kwargs)


def test_schema_is_a_required_keyword():
    with pytest.raises(TypeError):
        make_object_id("table", "employees")  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [
    "EMPLOYEES",                         # bare name (today's parser output)
    "procedure:calc_bonus",              # no schema (contract 0.1)
    "table:LEGACY.EMPLOYEES",            # not lower-case
    "table:legacy.pkg.employees",        # tables have no package
    "table:legacy.employees(number)",    # tables have no signature
    "procedure:a.b.c.d",                 # too many parts
    "synonym:legacy.emp",                # unknown kind
    "procedure:hr.pkg.calc(number, date)",  # spaces in signature
    "unresolved:legacy.fn(number)",      # unresolved ids carry no signature
    "",
])
def test_invalid_ids_are_rejected(bad):
    assert not is_object_id(bad)
    with pytest.raises(ValueError):
        parse_object_id(bad)


# ---------------------------------------------------------------- unresolved

def test_one_part_unresolved_reference_gets_the_default_schema():
    assert make_unresolved_id("BONUS_RULES", default_schema="legacy") == "unresolved:legacy.bonus_rules"


@pytest.mark.parametrize("reference, expected", [
    ("pkg.fn", "unresolved:pkg.fn"),                  # ambiguous: kept, no schema invented
    ("HR.BONUS_RULES", "unresolved:hr.bonus_rules"),
    ("hr.pkg.fn", "unresolved:hr.pkg.fn"),
])
def test_qualified_unresolved_references_are_kept_as_written(reference, expected):
    assert make_unresolved_id(reference, default_schema="legacy") == expected


def test_unresolved_ids_are_not_interpreted_when_parsed():
    ref = parse_object_id("unresolved:pkg.fn")
    assert ref == ObjectRef("unresolved", None, None, "pkg.fn", None)


def test_unresolved_reference_with_too_many_parts_is_rejected():
    with pytest.raises(ValueError):
        make_unresolved_id("a.b.c.d", default_schema="legacy")


# ---------------------------------------------------------------- collisions

def test_quoted_and_unquoted_names_that_collide_raise_a_clear_error():
    entries = [
        (make_object_id("table", '"Emp"', schema="legacy"), '"Emp" in 01_schema.sql'),
        (make_object_id("table", "EMP", schema="legacy"), "EMP in 01_schema.sql"),
        (make_object_id("table", "dept", schema="legacy"), "DEPT in 01_schema.sql"),
    ]
    with pytest.raises(ObjectIdCollisionError) as err:
        check_unique_object_ids(entries)
    assert err.value.collisions == {"table:legacy.emp": ['"Emp" in 01_schema.sql', "EMP in 01_schema.sql"]}
    assert "table:legacy.emp" in str(err.value)


def test_unique_ids_pass():
    check_unique_object_ids([("table:legacy.emp", "a"), ("table:legacy.dept", "b"),
                             ("procedure:legacy.emp", "c")])  # same name, different kind


def test_overloads_are_not_collisions():
    check_unique_object_ids([("procedure:hr.pkg.calc(number)", "a"), ("procedure:hr.pkg.calc(date)", "b")])


# ---------------------------------------------------------------- TriggerInfo

def test_trigger_info_for_the_sample_trigger():
    info = TriggerInfo(table="table:legacy.employees", timing="after", events=["update"],
                       update_columns=["BASE_SALARY"], for_each_row=True)
    assert info.update_columns == ["base_salary"]


@pytest.mark.parametrize("kwargs", [
    dict(table="EMPLOYEES", timing="after", events=["update"]),                   # raw name
    dict(table="procedure:legacy.calc_bonus", timing="after", events=["update"]), # not a table
    dict(table="unresolved:legacy.employees", timing="after", events=["update"]), # target must be resolved
    dict(table="table:legacy.employees", timing="after", events=[]),               # no event
    dict(table="table:legacy.employees", timing="after", events=["update", "update"]),
    dict(table="table:legacy.employees", timing="later", events=["update"]),
    dict(table="table:legacy.employees", timing="after", events=["truncate"]),
])
def test_trigger_info_rejects_invalid_metadata(kwargs):
    with pytest.raises(ValidationError):
        TriggerInfo(**kwargs)


def test_instead_of_trigger_on_a_view_is_allowed():
    TriggerInfo(table="view:legacy.org_chart", timing="instead_of", events=["insert", "delete"])
