
from pathlib import Path

import pytest

from app.contracts.models import ParamMode
from app.parsers.oracle_parser import parse_sources, summarize_objects


# Project root is two levels above this test file.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORACLE_SAMPLES_DIR = PROJECT_ROOT / "samples" / "oracle"


@pytest.fixture(scope="module")
def oracle_sources():
    """Load the actual Oracle sample SQL files."""

    files = sorted(ORACLE_SAMPLES_DIR.glob("*.sql"))

    assert len(files) == 7, (
        f"Expected 7 Oracle SQL files, found {len(files)}"
    )

    return {
        file.name: file.read_text(encoding="utf-8")
        for file in files
    }


@pytest.fixture(scope="module")
def parsed_objects(oracle_sources):
    """Parse all seven Oracle sample files once."""

    return parse_sources(oracle_sources)


def test_discovers_16_database_objects(parsed_objects):
    assert len(parsed_objects) == 16


def test_discovers_expected_object_types(parsed_objects):
    expected = {
        "table": 4,
        "sequence": 2,
        "package": 1,
        "function": 3,
        "procedure": 4,
        "trigger": 1,
        "view": 1,
    }

    actual = summarize_objects(parsed_objects)

    assert actual == expected


def test_discovers_all_expected_tables(parsed_objects):
    names = {
        obj.name
        for obj in parsed_objects
        if obj.type.value == "table"
    }

    assert names == {
        "departments",
        "employees",
        "payroll_runs",
        "salary_audit",
    }


def test_package_specification_and_body_are_merged(parsed_objects):
    packages = [
        obj for obj in parsed_objects
        if obj.type.value == "package"
    ]

    assert len(packages) == 1

    package = packages[0]

    assert package.name == "payroll_pkg"
    assert len(package.source_code) > 0


def test_discovers_expected_functions(parsed_objects):
    names = {
        obj.name
        for obj in parsed_objects
        if obj.type.value == "function"
    }

    assert names == {
        "get_emp_grade",
        "top_earners",
        "net_pay",
    }


def test_discovers_expected_procedures(parsed_objects):
    names = {
        obj.name
        for obj in parsed_objects
        if obj.type.value == "procedure"
    }

    assert names == {
        "calc_bonus",
        "apply_raise",
        "monthly_payroll",
        "close_month",
    }


def test_package_members_have_canonical_object_ids(parsed_objects):
    package_members = [
        obj for obj in parsed_objects
        if obj.name in {"net_pay", "close_month"}
    ]

    assert len(package_members) == 2

    object_ids = {obj.object_id for obj in package_members}

    assert "function:legacy.payroll_pkg.net_pay" in object_ids
    assert "procedure:legacy.payroll_pkg.close_month" in object_ids


def test_extracts_function_return_types(parsed_objects):
    functions = {
        obj.name: obj
        for obj in parsed_objects
        if obj.type.value == "function"
    }

    assert functions["get_emp_grade"].return_type == "VARCHAR2"
    assert functions["top_earners"].return_type == "VARCHAR2"


def test_extracts_procedure_parameters(parsed_objects):
    procedures = {
        obj.name: obj
        for obj in parsed_objects
        if obj.type.value == "procedure"
    }

    calc_bonus = procedures["calc_bonus"]

    parameter_names = {
        param.name for param in calc_bonus.params
    }

    assert parameter_names == {
        "p_emp_id",
        "p_override",
        "p_bonus",
    }

    bonus_output = next(
        param for param in calc_bonus.params
        if param.name == "p_bonus"
    )

    assert bonus_output.mode == ParamMode.OUT


def test_extracts_oracle_specific_features(parsed_objects):
    views = [
        obj for obj in parsed_objects
        if obj.type.value == "view"
    ]

    assert len(views) == 1

    features = views[0].oracle_features

    assert "CONNECT_BY" in features
    assert "START_WITH" in features
    assert "SYS_CONNECT_BY_PATH" in features


def test_extracts_table_reads_and_writes(parsed_objects):
    procedures = {
        obj.name: obj
        for obj in parsed_objects
        if obj.type.value == "procedure"
    }

    apply_raise = procedures["apply_raise"]

    assert "employees" in apply_raise.reads_tables
    assert "employees" in apply_raise.writes_tables


def test_preserves_original_source_code(parsed_objects):
    for obj in parsed_objects:
        assert obj.source_code
        assert len(obj.source_code.strip()) > 0
        assert obj.source_file.endswith(".sql")
