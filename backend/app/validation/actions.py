"""Trigger-test actions: the one place a test runs a SQL statement of its own (step 7b, D-7b.2).

A trigger cannot be called; it is fired by DML. So a trigger test carries ``action``,
one fixed statement such as ``UPDATE employees SET base_salary = 70000 WHERE emp_id = 106``.
Because that is SQL from outside the converted object, both runners accept it only when
every check below passes, independently of where the test case came from:

1. **Trusted origin.** ``origin`` is ``golden`` or ``rule``, never ``llm``.
2. **Defined in our own code.** The statement is one of ``trusted_actions()``: the actions
   of the golden tests in ``golden_tests.py``. A test case loaded from JSON, an upload or an
   LLM cannot introduce a new statement by setting ``origin``; at most it can reuse one of
   ours. (Rule-generated tests, step 7c, will register theirs here in code.)
3. **One plain DML statement.** It starts with INSERT, UPDATE or DELETE; there is no second
   statement, no comment, and no DDL, transaction-control, call or privilege keyword
   outside string literals.

The runners then execute it inside the test's transaction, which is always rolled back;
the PostgreSQL runner also requires the connection to be ``pv_runner`` (no DDL rights).
"""

from __future__ import annotations

import re
from functools import lru_cache

from app.contracts.models import TestCase

ALLOWED_VERBS = ("INSERT", "UPDATE", "DELETE")
TRUSTED_ORIGINS = frozenset({"golden", "rule"})

_STRING = re.compile(r"'(?:[^']|'')*'")
_FORBIDDEN = re.compile(
    r"\b(CREATE|ALTER|DROP|TRUNCATE|RENAME|GRANT|REVOKE|COMMIT|ROLLBACK|SAVEPOINT|BEGIN|DECLARE|"
    r"EXECUTE|EXEC|CALL|COPY|MERGE|LOCK|VACUUM|LOAD|LISTEN|NOTIFY|PREPARE|DEALLOCATE|IMMEDIATE|"
    r"DBMS_\w+|UTL_\w+|PG_\w+|LO_\w+|DBLINK\w*)\b",
    re.IGNORECASE,
)


class UnsafeAction(ValueError):
    """The test's action is not a trusted, single DML statement; the test cannot be run."""


@lru_cache(maxsize=1)
def trusted_actions() -> frozenset[str]:
    """The action statements defined in this repository's golden tests."""
    from app.validation.golden_tests import GOLDEN_TESTS  # late import: golden_tests imports contracts only

    return frozenset(_normalise(c.action) for cases in GOLDEN_TESTS.values() for c in cases if c.action)


def _normalise(statement: str) -> str:
    text = statement.strip()
    return text[:-1].rstrip() if text.endswith(";") else text


def check_statement(statement: str) -> str:
    """The statement without a trailing ';', if it is exactly one plain INSERT/UPDATE/DELETE."""
    text = _normalise(statement or "")
    if not text:
        raise UnsafeAction("the action is empty")
    if "\x00" in text:
        raise UnsafeAction("the action contains a NUL byte")
    code = _STRING.sub(" 0 ", text)  # judge the SQL around string literals, not their contents
    if "'" in code:
        raise UnsafeAction("the action has an unterminated string literal")
    if "--" in code or "/*" in code or "*/" in code:
        raise UnsafeAction("the action contains a comment")
    if ";" in code:
        raise UnsafeAction("the action contains more than one statement")
    verb = code.split(None, 1)[0].upper()
    if verb not in ALLOWED_VERBS:
        raise UnsafeAction(f"the action must be INSERT, UPDATE or DELETE, not {verb}")
    found = _FORBIDDEN.search(code)
    if found:
        raise UnsafeAction(f"the action uses {found.group(0).upper()!r}, which is not allowed")
    return text


def checked_action(case: TestCase) -> str:
    """The statement to run for ``case``, after every check in the module docstring."""
    if case.action is None:
        raise UnsafeAction(f"{case.test_id} has no action")
    if case.origin not in TRUSTED_ORIGINS:
        raise UnsafeAction(f"{case.test_id}: actions are only run for golden or rule tests, not {case.origin!r}")
    statement = check_statement(case.action)
    if statement not in trusted_actions():
        raise UnsafeAction(f"{case.test_id}: the action is not one defined in this repository's tests")
    return statement
