
from app.parsers.oracle_parser import (
    extract_params,
    extract_table_references,
    parse_sources,
    remove_comments,
    split_top_level_commas,
)


def test_comment_containing_sql_keywords_is_removed():
    sql = """
    CREATE TABLE employees (id NUMBER);
    -- CREATE TABLE fake_table (id NUMBER);
    /* CREATE PROCEDURE fake_proc AS BEGIN NULL; END; */
    """

    cleaned = remove_comments(sql)

    assert "employees" in cleaned
    assert "fake_table" not in cleaned
    assert "fake_proc" not in cleaned


def test_keywords_inside_string_literals_are_preserved():
    sql = """
    INSERT INTO messages VALUES (
        'This is not a -- comment or /* comment */'
    );
    """

    cleaned = remove_comments(sql)

    assert "'This is not a -- comment or /* comment */'" in cleaned


def test_split_commas_ignores_commas_inside_strings():
    value = "p_name VARCHAR2(100), p_message VARCHAR2(200) DEFAULT 'Hello, World'"

    result = split_top_level_commas(value)

    assert len(result) == 2
    assert result[0] == "p_name VARCHAR2(100)"
    assert result[1] == "p_message VARCHAR2(200) DEFAULT 'Hello, World'"


def test_split_commas_handles_escaped_single_quotes():
    value = "p_message VARCHAR2(100) DEFAULT 'John''s salary, approved', p_id NUMBER"

    result = split_top_level_commas(value)

    assert len(result) == 2
    assert result[0].endswith("'John''s salary, approved'")
    assert result[1] == "p_id NUMBER"


def test_split_commas_handles_nested_parentheses():
    value = "p_amount NUMBER(10,2), p_name VARCHAR2(100)"

    result = split_top_level_commas(value)

    assert len(result) == 2


def test_extract_params_handles_precision_and_scale():
    source = """
    CREATE OR REPLACE PROCEDURE calculate_salary(
        p_amount IN NUMBER(10,2),
        p_bonus OUT NUMBER(8,2)
    )
    AS
    BEGIN
        NULL;
    END;
    """

    params = extract_params(source, "PROCEDURE")

    assert len(params) == 2
    assert params[0].oracle_type == "NUMBER(10,2)"
    assert params[1].oracle_type == "NUMBER(8,2)"


def test_extract_params_handles_default_string_with_comma():
    source = """
    CREATE OR REPLACE PROCEDURE greet(
        p_message IN VARCHAR2 DEFAULT 'Hello, World',
        p_id IN NUMBER
    )
    AS
    BEGIN
        NULL;
    END;
    """

    params = extract_params(source, "PROCEDURE")

    assert len(params) == 2
    assert params[0].has_default
    assert params[0].default_expr == "'Hello, World'"


def test_extracts_schema_qualified_table_references():
    source = """
    SELECT *
    FROM HR.EMPLOYEES e
    JOIN HR.DEPARTMENTS d
      ON e.DEPARTMENT_ID = d.DEPARTMENT_ID;
    """

    reads, writes = extract_table_references(source)

    assert "HR.EMPLOYEES" in reads
    assert "HR.DEPARTMENTS" in reads
    assert writes == []

def test_extracts_schema_qualified_insert_target():
    source = """
    INSERT INTO HR.EMPLOYEES (EMPLOYEE_ID, NAME)
    VALUES (101, 'Aarsh');
    """

    reads, writes = extract_table_references(source)

    assert "HR.EMPLOYEES" in writes

def test_trigger_update_of_does_not_extract_of_as_table():
    source = """
    CREATE OR REPLACE TRIGGER trg_employee_salary
    BEFORE UPDATE OF salary ON employees
    FOR EACH ROW
    BEGIN
        NULL;
    END;
    """

    reads, writes = extract_table_references(source)

    assert "OF" not in reads
    assert "OF" not in writes
    assert "EMPLOYEES" in writes
