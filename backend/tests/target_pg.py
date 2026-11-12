"""Shared fixtures for tests that need a real PostgreSQL (target sandbox).

Each test module gets its own throwaway database, created and dropped here, so
the real ``target`` database is never touched. Needs a superuser connection:

    PV_TEST_ADMIN_URL=postgresql://sandbox:sandbox@localhost:5433/postgres

(the default, which is target-db from docker-compose). If it isn't reachable,
the tests that use these fixtures are skipped.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import make_conninfo  # noqa: E402

from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles  # noqa: E402

ADMIN_URL = os.getenv("PV_TEST_ADMIN_URL", "postgresql://sandbox:sandbox@localhost:5433/postgres")
DEPLOYER_PASSWORD = "pv_deployer"
RUNNER_PASSWORD = "pv_runner"

SANDBOX_TABLES = """
CREATE TABLE employees (
  emp_id      numeric(6)    PRIMARY KEY,
  full_name   varchar(100)  NOT NULL,
  base_salary numeric(10,2) NOT NULL
);
CREATE TABLE salary_audit (
  audit_id    numeric PRIMARY KEY,
  emp_id      numeric(6),
  old_salary  numeric(10,2),
  new_salary  numeric(10,2)
);
CREATE SEQUENCE salary_audit_seq START WITH 1;
INSERT INTO employees VALUES (101, 'Asha Verma', 120000), (102, 'Rohan Mehta', 85000.50);
"""


def _admin_connect(dbname: str | None = None):
    info = make_conninfo(ADMIN_URL, dbname=dbname) if dbname else ADMIN_URL
    return psycopg.connect(info, autocommit=True, connect_timeout=3)


@pytest.fixture(scope="module")
def scratch_db():
    """A fresh database with sandbox tables and both restricted roles set up."""
    try:
        server = _admin_connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL for target tests not reachable ({type(exc).__name__})")

    name = f"pv_test_{uuid.uuid4().hex[:8]}"
    server.execute(f'CREATE DATABASE "{name}"')
    try:
        with _admin_connect(name) as admin:
            admin.execute(SANDBOX_TABLES)
            admin.autocommit = False
            ensure_roles(admin, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)
        yield name
    finally:
        server.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        server.execute(f'DROP DATABASE IF EXISTS "{name}"')
        server.close()


def connect_as(dbname: str, role: str, *, autocommit: bool = False):
    password = {DEPLOYER_ROLE: DEPLOYER_PASSWORD, RUNNER_ROLE: RUNNER_PASSWORD}[role]
    info = make_conninfo(ADMIN_URL, dbname=dbname, user=role, password=password)
    return psycopg.connect(info, autocommit=autocommit, connect_timeout=3)


@pytest.fixture
def admin(scratch_db):
    with _admin_connect(scratch_db) as conn:
        yield conn


@pytest.fixture
def deployer_conn(scratch_db):
    with connect_as(scratch_db, DEPLOYER_ROLE) as conn:
        yield conn


@pytest.fixture
def runner_conn(scratch_db):
    with connect_as(scratch_db, RUNNER_ROLE) as conn:
        yield conn
