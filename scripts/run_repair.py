"""Run objects through the repair loop: convert, validate on Oracle and PostgreSQL,
send failures back, retry up to 3 times.

Default: the SCRIPTED STAND-IN converter (prepared files in samples/postgres/repairs/),
on the four objects it has prepared repairs for. From the repo root on your laptop:
    python scripts/seed_oracle.py
    python scripts/seed_target.py              # deploys the careless conversions
    python scripts/run_repair.py all           # or one object: calc_bonus
    python scripts/run_repair.py all --json repair_runs.json

--converter real (step 7b): the real OracleToPostgresConverter on all 8 sample objects,
deploying as pv_deployer and validating as pv_runner against live Oracle:
    python scripts/seed_oracle.py
    python scripts/seed_target.py --schema-only    # tables, sequences, data; no stand-ins
    python scripts/setup_target_roles.py
    python scripts/run_repair.py all --converter real
    python scripts/run_repair.py all --converter real --gemini   # opt-in: paid Gemini calls

Gemini is OFF unless --gemini is given AND GEMINI_API_KEY is configured. Without it,
org_chart (CONNECT BY, no deterministic rule) is reported as needing Gemini, which is the
honest result, not a failure of the object. The run changes the target database; run
scripts/seed_target.py again to reset it.

Exit codes (both converters; the last line printed says which and why):
    0  ALL PASSED      every object passed its golden tests and nothing awaits sign-off
    3  NEEDS SIGN-OFF  no object needs a human to continue, but at least one passed with a
                       flag for human review (PASSED_NEEDS_REVIEW) or ran no test at all
    1  NEEDS HUMAN     at least one object stopped: tests still fail, no repair route, the
                       harness could not judge it, or it needs Gemini (takes precedence over 3)
    2  NOT RUN         bad arguments, unknown object, --gemini without a key, or a target
                       not prepared for the real converter; nothing was converted
Only 0 means "fully passed, nothing outstanding". 3 is not a failure, but it is not done.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.contracts import ObjectType  # noqa: E402
from app.orchestration.repair_loop import (  # noqa: E402
    RepairDeps,
    RepairOutcome,
    run_repair_loop,
)
from app.orchestration.run_order import in_test_order  # noqa: E402
from app.orchestration.scripted_converter import ScriptedConverter  # noqa: E402
from app.parsers.oracle_parser import parse_sources  # noqa: E402
from app.validation.golden_tests import (  # noqa: E402
    GOLDEN_TESTS,
    MASKED_COLUMNS,
    golden_tests_for,
)
from app.validation.harness import validate_object  # noqa: E402

ORACLE_USER = os.getenv("ORACLE_USER", "legacy")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "legacy")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")
TARGET_URL = os.getenv("TARGET_DATABASE_URL_HOST", "postgresql://sandbox:sandbox@localhost:5433/target")
TARGET_DEPLOYER_URL = os.getenv("TARGET_DEPLOYER_URL", "postgresql://pv_deployer:pv_deployer@localhost:5433/target")
TARGET_RUNNER_URL = os.getenv("TARGET_RUNNER_URL", "postgresql://pv_runner:pv_runner@localhost:5433/target")
REPAIRS = ROOT / "samples" / "postgres" / "repairs"

EXIT_ALL_PASSED, EXIT_NEEDS_HUMAN, EXIT_NOT_RUN, EXIT_NEEDS_SIGN_OFF = 0, 1, 2, 3

OUTCOME_LABEL = {
    RepairOutcome.PASSED: "PASSED",
    RepairOutcome.PASSED_NEEDS_REVIEW: "PASSED, needs sign-off",
    RepairOutcome.NEEDS_HUMAN: "NEEDS HUMAN",
}


def prepared_repairs(objects) -> dict[str, list[Path]]:
    """object_id -> samples/postgres/repairs/<name>.1.sql, <name>.2.sql, ..."""
    return {o.object_id: sorted(REPAIRS.glob(f"{o.name.lower()}.*.sql")) for o in objects}


def describe_source(version: int, prepared: int) -> str:
    if version == 1:
        return "as deployed by seed_target.py"
    if version - 1 <= prepared:
        return f"prepared repair {version - 1}"
    return "no prepared repair left, same code"


def show(run, prepared: int | None) -> None:
    print(f"\n{run.object_id}")
    for a in run.attempts:
        if prepared is None:  # the real converter
            source = f"{a.tool.value}" + (f", {a.model}" if a.model else "") + (", needs sign-off" if a.needs_review
                                                                               else "")
        else:
            source = describe_source(a.version, prepared)
        if not a.compiled_ok:
            code = f" [{a.failure_code}]" if a.failure_code else ""
            print(f"  attempt {a.attempt}  ({source})  did not deploy{code}: {a.compile_error}")
            continue
        verdict = f"{a.passed} passed, {a.failed} failed" + (f", {a.errors} harness errors" if a.errors else "")
        print(f"  attempt {a.attempt}  ({source})  {verdict}  [{a.status.value}]")
        if a.feedback_summary:
            for line in a.feedback_summary.splitlines()[1:]:
                print(f"      {line}")
    print(f"  -> {OUTCOME_LABEL[run.outcome]}: {run.reason}")


def foreign_routines(conn) -> list[str]:
    """Routines, views or triggers in the target that pv_deployer does not own (e.g. seeded stand-ins)."""
    rows = conn.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND pg_get_userbyid(p.proowner) <> 'pv_deployer' "
        "UNION ALL SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'v' AND n.nspname = 'public' AND pg_get_userbyid(c.relowner) <> 'pv_deployer'"
    ).fetchall()
    return sorted(r[0] for r in rows)


def fully_passed(run) -> bool:
    """PASSED on tests that actually ran: at least one, all of them passing, no review flag."""
    tested = run.final_validation
    return (run.outcome == RepairOutcome.PASSED and tested is not None and len(tested.tests) > 0
            and tested.passed == len(tested.tests))


def exit_status(runs) -> tuple[int, str]:
    """The process exit code for these runs, and one line saying why (see the module docstring)."""
    if not runs:
        return EXIT_NOT_RUN, "Exit 2 (NOT RUN): no object was run."
    stopped = [r.object_id for r in runs if r.outcome == RepairOutcome.NEEDS_HUMAN]
    if stopped:
        return EXIT_NEEDS_HUMAN, f"Exit 1 (NEEDS HUMAN): {len(stopped)} object(s) stopped: {', '.join(stopped)}."
    outstanding = [r.object_id for r in runs if not fully_passed(r)]
    if outstanding:
        return EXIT_NEEDS_SIGN_OFF, (f"Exit 3 (NEEDS SIGN-OFF): no hard failure, but {len(outstanding)} object(s) "
                                     f"await review or testing: {', '.join(outstanding)}.")
    return EXIT_ALL_PASSED, f"Exit 0 (ALL PASSED): all {len(runs)} object(s) passed, nothing outstanding."


def finish(runs) -> int:
    code, why = exit_status(runs)
    print(why)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("object", help="object name, or 'all' for every object with golden tests")
    parser.add_argument("--json", type=Path, help="also write the runs to this JSON file")
    parser.add_argument("--converter", choices=["stand-in", "real"], default="stand-in",
                        help="stand-in (default): prepared repair files; real: OracleToPostgresConverter")
    parser.add_argument("--gemini", action="store_true",
                        help="with --converter real: allow paid Gemini calls (needs GEMINI_API_KEY)")
    args = parser.parse_args()
    if args.gemini and args.converter != "real":
        print("--gemini only applies to --converter real.")
        return EXIT_NOT_RUN
    if args.gemini and not settings.GEMINI_API_KEY:
        print("--gemini was given, but GEMINI_API_KEY is not configured. Nothing was run.")
        return EXIT_NOT_RUN

    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted((ROOT / "samples" / "oracle").glob("*.sql"))}
    objects = parse_sources(sources)
    by_id = {o.object_id: o for o in objects}
    tables = sorted(o.name.lower() for o in objects if o.type == ObjectType.TABLE)
    wanted = args.object.lower()
    if args.converter == "real":
        targets = [o for o in in_test_order(objects, golden_tests_for)
                   if wanted in ("all", o.name.lower(), (o.package or "").lower())]
    else:
        targets = [o for o in objects if o.object_id in GOLDEN_TESTS and wanted in ("all", o.name.lower())
                   and prepared_repairs([o])[o.object_id]]
        targets.sort(key=lambda o: list(GOLDEN_TESTS).index(o.object_id))
    if not targets:
        print(f"No object named '{wanted}' with golden tests. Nothing was run.")
        return EXIT_NOT_RUN

    import oracledb
    import psycopg

    if args.converter == "real":
        return run_real(args, objects, by_id, tables, targets, oracledb, psycopg)

    runs = []
    with oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN) as ora, \
            psycopg.connect(TARGET_URL) as pg:

        def validate(obj, cases, version):
            return validate_object(ora, pg, obj, cases, conversion_version=version,
                                   watch_tables=tables, masked_columns=MASKED_COLUMNS)

        repairs = prepared_repairs(targets)
        deps = RepairDeps(converter=ScriptedConverter(pg, repairs), validate=validate)
        print("Converter: SCRIPTED STAND-IN (prepared files), not the real converter.")
        for obj in targets:
            run = run_repair_loop(deps, obj, golden_tests_for(obj.object_id))
            show(run, len(repairs[obj.object_id]))
            runs.append(run)

    print("\nSummary")
    for run in runs:
        print(f"  {run.object_id:32} {len(run.attempts)} attempt(s)  {OUTCOME_LABEL[run.outcome]}")
    print("\nThe target database now holds the repaired versions. "
          "Run scripts/seed_target.py to reset it.\n")

    if args.json:
        args.json.write_text(json.dumps([r.model_dump(mode="json") for r in runs], indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
    return finish(runs)


def run_real(args, objects, by_id, tables, targets, oracledb, psycopg) -> int:
    from app.migration.converter import OracleToPostgresConverter
    from app.migration.llm_fallback import GeminiFallback
    from app.migration.postgres_deployer import PostgresDeployer

    runs = []
    with oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN) as ora, \
            psycopg.connect(TARGET_DEPLOYER_URL) as deployer_conn, psycopg.connect(TARGET_RUNNER_URL) as runner:
        foreign = foreign_routines(deployer_conn)
        deployer_conn.rollback()
        if foreign:
            print("The target holds objects pv_deployer does not own, so the real converter cannot replace them:")
            print("   " + ", ".join(foreign))
            print("Reset it first: python scripts/seed_target.py --schema-only, then scripts/setup_target_roles.py")
            return EXIT_NOT_RUN

        def validate(obj, cases, version):
            return validate_object(ora, runner, obj, cases, conversion_version=version,
                                   watch_tables=tables, masked_columns=MASKED_COLUMNS, objects=by_id)

        database = deployer_conn.execute("SELECT current_database()").fetchone()[0]
        deployer_conn.rollback()
        llm = GeminiFallback() if args.gemini else None
        converter = OracleToPostgresConverter(PostgresDeployer(deployer_conn, database=database), objects, llm=llm)
        print("Converter: REAL (OracleToPostgresConverter). "
              + (f"Gemini: ON, live calls (model {settings.GEMINI_MODEL})." if llm else "Gemini: OFF (no paid calls)."))
        deps = RepairDeps(converter=converter, validate=validate)
        for obj in targets:
            run = run_repair_loop(deps, obj, golden_tests_for(obj.object_id))
            show(run, None)
            runs.append(run)

    print("\nSummary (real converter)")
    for run in runs:
        tested = run.last_validation
        score = f"{tested.passed}/{len(tested.tests)} tests" if tested else "not tested"
        note = f"  ({run.attempts[-1].failure_code})" if run.attempts[-1].failure_code else ""
        print(f"  {run.object_id:40} {len(run.attempts)} attempt(s)  {score:12} {OUTCOME_LABEL[run.outcome]}{note}")
    print("\nThe target database now holds the real converter's output. "
          "Run scripts/seed_target.py again to reset it.\n")
    if args.json:
        args.json.write_text(json.dumps([r.model_dump(mode="json") for r in runs], indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
    return finish(runs)


if __name__ == "__main__":
    sys.exit(main())
