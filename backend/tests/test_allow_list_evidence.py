"""Translation regression tests for allow-list entries the samples do not use (PR 1, G3).

Each case pins the deterministic SQL the translator emits: these statements come out
unchanged. This is TRANSLATION_TEST evidence only. It does not run anything on Oracle or
PostgreSQL and is not evidence that the two behave the same; BEHAVIORAL_TEST evidence comes
with PR 1b. allow_list.py cites this test, and test_input_safety_g3.py checks the citation.
"""

from __future__ import annotations

import pytest

from app.migration.sql_translator import translate_sql

CASES = [
    # GROUP, HAVING, ASC
    ("SELECT dept_id, COUNT(*) FROM employees GROUP BY dept_id HAVING COUNT(*) > 1 ORDER BY dept_id ASC",
     "SELECT dept_id, COUNT(*) FROM employees GROUP BY dept_id HAVING COUNT(*) > 1 ORDER BY dept_id ASC"),
    # DISTINCT, LIKE, BETWEEN
    ("SELECT DISTINCT dept_id FROM employees WHERE full_name LIKE 'A%' AND base_salary BETWEEN 1000 AND 2000",
     "SELECT DISTINCT dept_id FROM employees WHERE full_name LIKE 'A%' AND base_salary BETWEEN 1000 AND 2000"),
    # UNION, ALL
    ("SELECT emp_id FROM employees UNION ALL SELECT dept_id FROM departments",
     "SELECT emp_id FROM employees UNION ALL SELECT dept_id FROM departments"),
    # ANY, EXISTS
    ("SELECT emp_id FROM employees WHERE base_salary > ANY (SELECT base_salary FROM employees) "
     "AND EXISTS (SELECT 1 FROM departments)",
     "SELECT emp_id FROM employees WHERE base_salary > ANY (SELECT base_salary FROM employees) "
     "AND EXISTS(SELECT 1 FROM departments)"),
    # UPPER, LOWER, COALESCE
    ("SELECT UPPER(full_name), LOWER(email), COALESCE(email, full_name) FROM employees",
     "SELECT UPPER(full_name), LOWER(email), COALESCE(email, full_name) FROM employees"),
]


@pytest.mark.parametrize(("oracle", "postgres"), CASES)
def test_plain_sql_translates_unchanged(oracle, postgres):
    result = translate_sql(oracle)
    assert result.successful
    assert result.target_sql == postgres
