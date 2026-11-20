"""Build the MigrationRun sample fixture (contract 0.7) from a real run of the sample.

What runs is real: the parser, PreparedTargetSchema, the converter (Gemini OFF: no calls,
no key needed), the restricted deployer and the repair loop, on PostgreSQL. Only Oracle's
side of each test is its recorded answer (ORACLE_EXPECTED, checked against live Oracle 23
by scripts/run_validation.py), so no Oracle is needed. The run says so: oracle_answers is
"recorded". A run against live Oracle comes with migrate.py (M1).

Needs only the target-db container: it creates a throwaway database, loads
samples/postgres/01_schema.sql and 02_seed_data.sql, sets up the roles and the helper
(target_roles.ensure_roles, with the same role passwords the test suite uses), and drops
the database afterwards. The real target database is never touched.

From the repo root:
    python scripts/build_run_fixture.py
    python scripts/build_run_fixture.py -o reports/migration_run.json

Environment: PV_TEST_ADMIN_URL, a superuser URL (default as in backend/tests/target_pg.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts import (  # noqa: E402
    MigrationOptions,
    ObjectType,
    RunStatus,
    SourceFile,
    TargetEnvironment,
    TestStatus,
    ValidationResult,
    ValidationStatus,
)
from app.migration.converter import OracleToPostgresConverter  # noqa: E402
from app.migration.postgres_deployer import PostgresDeployer  # noqa: E402
from app.migration.prepared_target import PreparedTargetSchema  # noqa: E402
from app.migration.target_roles import ensure_roles  # noqa: E402
from app.orchestration.repair_loop import RepairDeps, run_repair_loop  # noqa: E402
from app.orchestration.run_order import in_test_order  # noqa: E402
from app.orchestration.run_result import build_migration_run  # noqa: E402
from app.parsers.oracle_parser import parse_sources  # noqa: E402
from app.validation.answer_key import judge  # noqa: E402
from app.validation.golden_tests import MASKED_COLUMNS, golden_tests_for  # noqa: E402
from app.validation.harness import _guarded  # noqa: E402
from app.validation.postgres_runner import run_postgres  # noqa: E402

ADMIN_URL = os.getenv("PV_TEST_ADMIN_URL", "postgresql://sandbox:sandbox@localhost:5433/postgres")
PASSWORDS = {"pv_deployer": "pv_deployer", "pv_runner": "pv_runner"}   # as backend/tests/target_pg.py
DEFAULT_OUT = ROOT / "backend" / "tests" / "fixtures" / "migration_run_sample.json"
ENGINE = "OracleToPostgresConverter + repair loop (scripts/build_run_fixture.py; Gemini off)"


def _url(base: str, *, database: str, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(base)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    if user:
        netloc = f"{user}:{password}@{netloc}"
    elif "@" in parts.netloc:
        netloc = parts.netloc
    return urlunsplit((parts.scheme, netloc, f"/{database}", "", ""))


def recorded_validate(runner, objects: dict, tables: list[str]):
    """Validate as harness.validate_object does, with Oracle's side from its recorded answers."""
    def validate(obj, cases, version) -> ValidationResult:
        outcomes = [judge(case, _guarded("postgres", run_postgres, runner, obj, case, tables, objects), MASKED_COLUMNS)
                    for case in cases]
        runner.rollback()
        passed, failed, errors = (sum(o.status is s for o in outcomes)
                                  for s in (TestStatus.PASSED, TestStatus.FAILED, TestStatus.ERROR))
        status = (ValidationStatus.REQUIRES_REPAIR if failed else
                  ValidationStatus.NEEDS_HUMAN if errors else ValidationStatus.PASSED)
        return ValidationResult(object_id=obj.object_id, conversion_version=version, tests=outcomes,
                                passed=passed, failed=failed, status=status)
    return validate


def build(admin_url: str) -> dict:
    import psycopg

    started = datetime.now(timezone.utc)
    sources = [SourceFile.of(p.name, p.read_text(encoding="utf-8"))
               for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))]
    objects = parse_sources({s.name: s.text for s in sources})
    by_id = {o.object_id: o for o in objects}
    tables = sorted(o.name.lower() for o in objects if o.type == ObjectType.TABLE)
    options = MigrationOptions()

    name = f"pv_fixture_{uuid.uuid4().hex[:8]}"
    server = psycopg.connect(admin_url, autocommit=True, connect_timeout=5)
    server.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(_url(admin_url, database=name), autocommit=True) as admin:
            for path in ("01_schema.sql", "02_seed_data.sql"):
                admin.execute((ROOT / "samples" / "postgres" / path).read_text(encoding="utf-8"))
            admin.autocommit = False
            ensure_roles(admin, deployer_password=PASSWORDS["pv_deployer"], runner_password=PASSWORDS["pv_runner"])
        env = TargetEnvironment(
            oracle_dsn="recorded-answers", oracle_user="legacy", oracle_password="not-used",
            deployer_url=_url(admin_url, database=name, user="pv_deployer", password=PASSWORDS["pv_deployer"]),
            runner_url=_url(admin_url, database=name, user="pv_runner", password=PASSWORDS["pv_runner"]),
            database_name=name)
        schema = PreparedTargetSchema().migrate(
            sources, env, expected_tables=[o.object_id for o in objects if o.type == ObjectType.TABLE],
            expected_sequences=[o.object_id for o in objects if o.type == ObjectType.SEQUENCE])
        repairs = {}
        if schema.ok:
            with psycopg.connect(env.deployer_url.get_secret_value()) as deployer, \
                    psycopg.connect(env.runner_url.get_secret_value()) as runner:
                converter = OracleToPostgresConverter(PostgresDeployer(deployer, database=name), objects)
                deps = RepairDeps(converter=converter, validate=recorded_validate(runner, by_id, tables),
                                  max_attempts=options.max_repair_attempts)
                for obj in in_test_order(objects, golden_tests_for):
                    repairs[obj.object_id] = run_repair_loop(deps, obj, golden_tests_for(obj.object_id))
    finally:
        server.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        server.execute(f'DROP DATABASE IF EXISTS "{name}"')
        server.close()

    run = build_migration_run(
        run_id=f"sample-{started:%Y%m%d%H%M%S}", started_at=started, finished_at=datetime.now(timezone.utc),
        engine=ENGINE, stand_in=False, oracle_answers="recorded", options=options, sources=sources,
        objects=objects, schema_result=schema, repairs=repairs, problems=list(schema.problems),
        status=RunStatus.COMPLETED if schema.ok else RunStatus.NOT_RUN)
    data = run.model_dump(mode="json")
    data["schema_result"]["database"] = "target"   # the throwaway name is noise in a committed fixture
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    data = build(ADMIN_URL)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    m = data["metrics"]["objects"]
    print(f"{data['status']}: {sum(m.values())} objects: {m['passed']} passed, {m['passed_needs_review']} need "
          f"sign-off, {m['needs_human']} need a human, {m['not_converted']} not converted, {m['error']} errors.")
    print(f"Wrote {args.output}")
    return 0 if data["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
