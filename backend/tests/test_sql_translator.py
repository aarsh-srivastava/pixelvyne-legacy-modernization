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


def test_sysdate_translates_to_statement_timestamp():
    # Stage 1 decision D2 (agreed with Aarsh): truncate to the second, then cast.
    result = translate_sql("SELECT SYSDATE FROM dual")

    assert result.successful
    assert result.target_sql == (
        "SELECT CAST(DATE_TRUNC('SECOND', STATEMENT_TIMESTAMP()) AS TIMESTAMP(0))"
    )


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


# --- Stage 2, step 3: the SQL rules (R1, R3, R5-R10, R13) --------------------------
# Tests below added on top of Aarsh's original seven.

import os  # noqa: E402
from datetime import datetime  # noqa: E402
from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from app.migration.sql_translator import translate_expression  # noqa: E402

CTX = dict(
    date_names={"hire_date", "p_run_date"},
    number_names={"base_salary"},
    known_functions={"get_emp_grade", "payroll_pkg.net_pay"},
    function_renames={"net_pay": "payroll_pkg.net_pay"},
    constants={"payroll_pkg.c_allowance": "2500", "g_tax_rate": "0.10"},
)


def ok(sql, **ctx):
    result = translate_sql(sql, **{**CTX, **ctx})
    assert result.successful, result.unsupported_features
    return result.target_sql


def refused(sql, **ctx):
    result = translate_sql(sql, **{**CTX, **ctx})
    assert not result.successful
    return " ".join(result.unsupported_features)


def test_empty_string_becomes_null():
    assert ok("SELECT emp_id FROM employees WHERE perf_rating = ''") == (
        "SELECT emp_id FROM employees WHERE perf_rating = NULL"
    )


def test_concatenation_is_null_safe_and_flattened():
    assert ok("SELECT 'Dr. ' || full_name || NULL FROM employees") == (
        "SELECT NULLIF(CONCAT('Dr. ', full_name, NULL), '') FROM employees"
    )


def test_trunc_on_dates_numbers_and_unknowns():
    sql = ok("SELECT TRUNC(hire_date), TRUNC(hire_date, 'MM'), TRUNC(hire_date, 'YYYY'), TRUNC(3.7), "
             "TRUNC(base_salary), TRUNC(base_salary, 2) FROM employees")
    assert sql == (
        "SELECT DATE_TRUNC('DAY', hire_date), DATE_TRUNC('MONTH', hire_date), DATE_TRUNC('YEAR', hire_date), "
        "TRUNC(3.7), TRUNC(base_salary), TRUNC(base_salary, 2) FROM employees"
    )
    assert "can't tell a date from a number" in refused("SELECT TRUNC(unknown_col) FROM t")
    assert "depends on Oracle settings" in refused("SELECT TRUNC(hire_date, 'DAY') FROM employees")


def test_trunc_of_sysdate_is_a_date():
    assert ok("SELECT TRUNC(SYSDATE) FROM dual").startswith("SELECT DATE_TRUNC('DAY', CAST(")


def test_sequences():
    assert ok("SELECT salary_audit_seq.NEXTVAL, payroll_run_seq.CURRVAL FROM dual") == (
        "SELECT NEXTVAL('salary_audit_seq'), CURRVAL('payroll_run_seq')"
    )


def test_package_constants_and_internal_calls():
    assert ok("SELECT payroll_pkg.c_allowance, NVL(SUM(net_pay(gross_pay)), 0) FROM payroll_runs") == (
        "SELECT 2500, COALESCE(SUM(payroll_pkg.net_pay(gross_pay)), 0) FROM payroll_runs"
    )
    assert translate_expression("ROUND(NVL(p_gross, 0) * (1 - g_tax_rate), 2)", **CTX).target_sql == (
        "ROUND(COALESCE(p_gross, 0) * (1 - 0.10), 2)"
    )


def test_casts_keep_oracle_meaning():
    assert ok("SELECT CAST(x AS DATE), CAST(y AS INTEGER), CAST(z AS NUMBER(10, 2)) FROM t") == (
        "SELECT CAST(x AS TIMESTAMP(0)), CAST(y AS DECIMAL(38, 0)), CAST(z AS DECIMAL(10, 2)) FROM t"
    )


def test_top_n_rownum_becomes_limit_where_the_order_by_is():
    sql = ok("SELECT full_name FROM (SELECT full_name FROM employees ORDER BY base_salary DESC, emp_id) "
             "WHERE ROWNUM <= p_n")
    assert sql == (
        "SELECT full_name FROM (SELECT full_name FROM employees ORDER BY base_salary DESC, emp_id "
        "LIMIT CAST(GREATEST(COALESCE(FLOOR(p_n), 0), 0) AS BIGINT))"
    )
    assert "CEIL(5) - 1" in ok("SELECT a FROM (SELECT a FROM t ORDER BY a) WHERE 5 > ROWNUM")


@pytest.mark.parametrize("sql", [
    "SELECT a FROM t WHERE ROWNUM <= 5",                                   # no ordered subquery
    "SELECT a FROM (SELECT a FROM t) WHERE ROWNUM <= 5",                   # subquery not ordered
    "SELECT a FROM (SELECT a FROM t ORDER BY a) WHERE ROWNUM <= 5 AND a > 1",
    "SELECT ROWNUM, a FROM (SELECT a FROM t ORDER BY a) WHERE ROWNUM <= 5",
    "SELECT a FROM (SELECT a FROM t ORDER BY a) WHERE ROWNUM >= 5",
])
def test_other_rownum_uses_stay_unsupported(sql):
    assert "ROWNUM" in refused(sql)


@pytest.mark.parametrize("sql, reason", [
    ("SELECT e.a FROM e, d WHERE e.id = d.id(+)", "(+) operator"),   # SQLGlot would make it an inner join
    ("SELECT emp_id FROM employees START WITH manager_id IS NULL CONNECT BY PRIOR emp_id = manager_id",
     "CONNECT BY"),
    ("SELECT * FROM employees@remote_db", "Database link"),
    ("SELECT TO_DATE('2026-01-01', 'YYYY-MM-DD') FROM dual", "TO_DATE"),
    ("SELECT MONTHS_BETWEEN(a, b) FROM t", "MONTHS_BETWEEN"),
    ("SELECT mystery(x) FROM t", "mystery()"),
    ("SELECT 1 FROM dual, employees", "DUAL"),
])
def test_unsafe_constructs_are_reported_not_emitted(sql, reason):
    assert reason in refused(sql)


def test_known_routines_and_native_functions_are_fine():
    ok("SELECT get_emp_grade(emp_id), UPPER(full_name), GREATEST(1, 2), payroll_pkg.net_pay(1) FROM employees")


def test_translate_expression():
    result = translate_expression("NVL(v_rating, 'U') || '-' || p_suffix", **CTX)
    assert result.successful
    assert result.target_sql == "NULLIF(CONCAT(COALESCE(v_rating, 'U'), '-', p_suffix), '')"


# --- the same rules, run in real PostgreSQL against Oracle's answers -------------------

ADMIN_URL = os.getenv("PV_TEST_ADMIN_URL", "postgresql://sandbox:sandbox@localhost:5433/postgres")


@pytest.fixture(scope="module")
def pg():
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable ({type(exc).__name__})")
    yield conn
    conn.close()


def run(pg, oracle_sql, **ctx):
    return pg.execute(ok(oracle_sql, **ctx)).fetchone()[0]


def d(text):
    return f"CAST(TIMESTAMP '{text}' AS DATE)"


@pytest.mark.parametrize("start, months, oracle_answer", [
    ("2026-11-30 10:00:00", -6, "2026-05-31 10:00:00"),   # month-end stays month-end (the payroll cut-off)
    ("2026-01-31 00:00:00", 1, "2026-02-28 00:00:00"),    # clamps to a shorter month
    ("2026-02-28 00:00:00", 1, "2026-03-31 00:00:00"),    # 28 Feb 2026 is a month-end
    ("2024-02-29 00:00:00", 12, "2025-02-28 00:00:00"),   # leap day
    ("2026-03-31 00:00:00", -1, "2026-02-28 00:00:00"),
    ("2026-01-15 08:30:00", 1, "2026-02-15 08:30:00"),    # time of day kept
    ("2026-01-15 00:00:00", 1.9, "2026-02-15 00:00:00"),  # fractional months truncated
    ("2026-01-15 00:00:00", -1.9, "2025-12-15 00:00:00"),
])
def test_add_months_matches_oracle(pg, start, months, oracle_answer):
    assert run(pg, f"SELECT ADD_MONTHS({d(start)}, {months}) FROM dual") == datetime.fromisoformat(oracle_answer)


def test_last_day_and_trunc_match_oracle(pg):
    assert run(pg, f"SELECT LAST_DAY({d('2026-02-10 14:20:00')}) FROM dual") == datetime(2026, 2, 28, 14, 20)
    assert run(pg, f"SELECT TRUNC({d('2026-11-13 15:45:10')}, 'MM') FROM dual") == datetime(2026, 11, 1)
    assert run(pg, f"SELECT TRUNC({d('2026-11-13 15:45:10')}) FROM dual") == datetime(2026, 11, 13)


def test_sysdate_has_whole_seconds_and_no_time_zone(pg):
    value = run(pg, "SELECT SYSDATE FROM dual")
    assert value.tzinfo is None and value.microsecond == 0


def test_sysdate_never_rounds_up(pg):
    # The rule truncates before casting; a plain ::timestamp(0) would give 10:00:01 here.
    template = ok("SELECT SYSDATE FROM dual").replace("STATEMENT_TIMESTAMP()", "TIMESTAMP '2026-11-13 10:00:00.6'")
    assert pg.execute(template).fetchone()[0] == datetime(2026, 11, 13, 10, 0, 0)


@pytest.mark.parametrize("op, n, rows", [
    ("<=", "2", 2), ("<=", "2.5", 2), ("<=", "0", 0), ("<=", "-1", 0), ("<=", "NULL", 0),
    ("<", "3", 2), ("<", "2.5", 2), ("<", "1", 0), ("<", "NULL", 0),
])
def test_top_n_matches_oracle_rownum(pg, op, n, rows):
    oracle = (f"SELECT x FROM (SELECT x FROM (SELECT 5 AS x FROM dual) t ORDER BY x DESC) "
              f"WHERE ROWNUM {op} {n}")
    sql = ok(oracle).replace("(SELECT 5 AS x) AS t", "generate_series(1, 5) AS t(x)")
    assert [r[0] for r in pg.execute(sql).fetchall()] == [5, 4][:rows]


@pytest.mark.parametrize("oracle_sql, oracle_answer", [
    ("SELECT 'a' || NULL FROM dual", "a"),
    ("SELECT NULL || NULL FROM dual", None),
    ("SELECT '' || '' FROM dual", None),
    ("SELECT 'x' || '' || 'y' FROM dual", "xy"),
])
def test_concatenation_matches_oracle(pg, oracle_sql, oracle_answer):
    assert run(pg, oracle_sql) == oracle_answer


def test_casts_match_oracle(pg):
    assert run(pg, "SELECT CAST(2.5 AS INTEGER) FROM dual") == Decimal("3")
    assert run(pg, f"SELECT CAST({d('2026-11-13 15:45:10')} AS DATE) FROM dual") == datetime(2026, 11, 13, 15, 45, 10)


# --- R5: date arithmetic in days ------------------------------------------------------

def test_date_minus_date_and_date_plus_days():
    ctx = dict(date_names={"v_month_end", "v_month_start", "hire_date"})
    assert translate_expression("v_month_end - v_month_start + 1", **ctx).target_sql == (
        "EXTRACT(EPOCH FROM (v_month_end - v_month_start)) / 86400 + 1"
    )
    assert translate_expression("v_month_end - GREATEST(TRUNC(e.hire_date), v_month_start)", **ctx).target_sql == (
        "EXTRACT(EPOCH FROM (v_month_end - GREATEST(DATE_TRUNC('DAY', e.hire_date), v_month_start))) / 86400"
    )
    assert translate_expression("hire_date + 7", **ctx).target_sql == "hire_date + (7) * INTERVAL '1 DAY'"
    assert translate_expression("7 + hire_date", **ctx).target_sql == "hire_date + (7) * INTERVAL '1 DAY'"
    assert translate_expression("base_salary - 1", **ctx).target_sql == "base_salary - 1"


@pytest.mark.parametrize("oracle_expr, oracle_answer", [
    (f"{d('2026-11-30 00:00:00')} - {d('2026-11-01 00:00:00')} + 1", Decimal("30")),   # days in November
    (f"{d('2026-11-13 12:00:00')} - {d('2026-11-13 00:00:00')}", Decimal("0.5")),      # fraction of a day
    (f"{d('2026-03-29 00:00:00')} - {d('2026-03-28 00:00:00')}", Decimal("1")),
])
def test_date_difference_matches_oracle(pg, oracle_expr, oracle_answer):
    assert run(pg, f"SELECT {oracle_expr} FROM dual") == oracle_answer


def test_date_plus_fractional_days_matches_oracle(pg):
    assert run(pg, f"SELECT {d('2026-11-13 00:00:00')} + 1.5 FROM dual") == datetime(2026, 11, 14, 12, 0)
    assert run(pg, f"SELECT {d('2026-11-13 00:00:00')} - 1 FROM dual") == datetime(2026, 11, 12, 0, 0)


# --- R4: division is exact decimal -------------------------------------------------------

def test_division_is_decimal_not_float():
    assert ok("SELECT a / 100 FROM t") == "SELECT CAST(a AS DECIMAL) / 100 FROM t"
    assert translate_expression("ROUND(v_old * (1 + p_pct / 100), 2)").target_sql == (
        "ROUND(v_old * (1 + CAST(p_pct AS DECIMAL) / 100), 2)"
    )
    assert "DOUBLE" not in ok("SELECT x / y / z FROM t")


@pytest.mark.parametrize("oracle_expr, oracle_answer", [
    ("1 / 3", Decimal("0.3333333333333333")),                # not integer division
    ("ROUND(85000.50 * (1 + 7 / 100), 2)", Decimal("90950.54")),  # ar-01: float would drift
    ("ROUND(10 / 4, 1)", Decimal("2.5")),
])
def test_division_matches_oracle(pg, oracle_expr, oracle_answer):
    value = run(pg, f"SELECT {oracle_expr} FROM dual")
    assert value == oracle_answer if "ROUND" in oracle_expr else round(value, 16) == oracle_answer
