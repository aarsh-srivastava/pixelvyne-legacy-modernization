from app.migration.sql_translator import translate_sql


def test_nvl_translates_to_coalesce():
    result = translate_sql("SELECT NVL(salary, 0) FROM employees")

    assert result.successful
    assert "COALESCE" in result.target_sql.upper()


def test_decode_translates_to_case():
    result = translate_sql(
        "SELECT DECODE(status, 1, 'ACTIVE', 'INACTIVE') FROM employees"
    )

    assert result.successful
    assert "CASE" in result.target_sql.upper()
    assert "'ACTIVE'" in result.target_sql


def test_sysdate_translates_to_current_timestamp():
    result = translate_sql("SELECT SYSDATE FROM dual")

    assert result.successful
    assert "CURRENT_TIMESTAMP" in result.target_sql.upper()


def test_dual_is_removed_from_simple_select():
    result = translate_sql("SELECT 1 FROM dual")

    assert result.successful
    assert "DUAL" not in result.target_sql.upper()
    assert "SELECT 1" in result.target_sql.upper()


def test_rownum_is_reported_as_unsupported():
    result = translate_sql(
        "SELECT employee_id, ROWNUM FROM employees"
    )

    assert not result.successful
    assert any("ROWNUM" in item for item in result.unsupported_features)


def test_empty_sql_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        translate_sql("")


def test_multiple_statements_are_rejected():
    result = translate_sql(
        "SELECT 1 FROM dual; SELECT 2 FROM dual"
    )

    assert not result.successful
