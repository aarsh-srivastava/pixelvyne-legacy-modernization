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
    python scripts/run_repair.py org_chart --converter real --with-edge-cases   # + generated cases (7d)

--with-edge-cases (step 7d, real converter only) adds the object's generated cases
(app/validation/generators.py, default seed and budget) to its golden tests, so the loop
also sees failures only a generated case finds, such as DEF-7c-1. They run live on Oracle
and PostgreSQL like the golden tests; the recorded corpus is not used or changed.

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
                       not prepared for the real converter (the pre-flight check below);
                       nothing was converted and Gemini was not called
Only 0 means "fully passed, nothing outstanding". 3 is not a failure, but it is not done.

Pre-flight (--converter real, approved by Aarsh on 17 Nov as F1): before the converter is built,
read-only catalogue checks confirm the deployer and runner connections really run as pv_deployer
and pv_runner on the database named by TARGET_DATABASE_NAME (default target; configured on its own,
never taken from the connection URLs), every sample table and sequence exists in public, both
roles hold the rights setup_target_roles.py grants (USAGE and CREATE checked separately), the
pv_compat helper is installed and usable, and, with --gemini, that Redis answers. Any failure
prints every problem with its fix and exits 2: no object is converted, no Gemini call is made.

With --gemini, each object's Gemini calls (and cache hits) are listed after the summary, and
with --json they are also written next to the report as <name>.calls.json: model, try, elapsed
time, outcome (ok, http_504, client_timeout, ...), timeout, prompt and response sizes, cache
status. Never prompt or response text, keys or URLs.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.contracts import ObjectType  # noqa: E402
from app.migration.compat import COMPAT_SCHEMA  # noqa: E402
from app.migration.prepared_target import (  # noqa: E402, F401 - the F1 pre-flight, moved there in 0.7
    DEPLOYER_TABLE_PRIVILEGES,
    PLATFORM_HELPER_ARGTYPES,
    RESET_HINT,
    ROLES_HINT,
    RUNNER_SEQUENCE_PRIVILEGES,
    RUNNER_TABLE_PRIVILEGES,
    foreign_routines,
    preflight,
)
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


def cases_for(object_id: str, with_edge_cases: bool = False) -> list:
    """The golden tests, plus (step 7d) the object's generated cases, golden first."""
    cases = list(golden_tests_for(object_id))
    if with_edge_cases:
        from app.validation.generators import generate

        cases += [c for c in generate() if c.object_id == object_id]
    return cases


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


def redis_problem(url: str) -> str | None:
    """None if Redis answers PING within 5 s; otherwise why (host and port only, never credentials)."""
    import redis

    parts = urlsplit(url)
    where = f"{parts.hostname or '?'}:{parts.port or 6379}"
    try:
        redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5).ping()
    except redis.RedisError as exc:
        return (f"Redis at {where} does not answer ({type(exc).__name__}); the Gemini cache is required before any "
                "Gemini call: docker compose up -d redis, and check REDIS_URL")
    return None


def show_calls(per_run: list[tuple]) -> None:
    """The Gemini calls (and cache hits) of each object's run."""
    print("\nGemini calls (per object; sizes only, never prompt or response text)")
    for run, calls in per_run:
        if not calls:
            print(f"  {run.object_id:40} no Gemini call")
            continue
        for c in calls:
            got = "-" if c.response_chars is None else c.response_chars
            print(f"  {run.object_id:40} try {c.attempt}  {c.elapsed_ms / 1000:7.1f} s  {c.outcome:16} "
                  f"timeout {c.timeout_s} s  prompt {c.prompt_chars}  response {got}  cache {c.cache}")


def calls_json(per_run: list[tuple]) -> list[dict]:
    return [{"object_id": run.object_id, "run_id": run.run_id, "calls": [asdict(c) for c in calls]}
            for run, calls in per_run]


def calls_path(report: Path) -> Path:
    """reports/x.json -> reports/x.calls.json"""
    return report.with_name(report.stem + ".calls.json")


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
    parser.add_argument("--with-edge-cases", action="store_true",
                        help="with --converter real: also run the object's generated cases (step 7d)")
    args = parser.parse_args()
    if args.with_edge_cases and args.converter != "real":
        print("--with-edge-cases only applies to --converter real.")
        return EXIT_NOT_RUN
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
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps([r.model_dump(mode="json") for r in runs], indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
    return finish(runs)


def run_real(args, objects, by_id, tables, targets, oracledb, psycopg) -> int:
    from app.migration.converter import OracleToPostgresConverter
    from app.migration.llm_fallback import GeminiFallback
    from app.migration.postgres_deployer import PostgresDeployer

    runs, per_run = [], []
    # Configured on its own (TARGET_DATABASE_NAME), never derived from the URLs being checked (Aarsh, 17 Nov).
    expected_db = settings.TARGET_DATABASE_NAME
    with oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN) as ora, \
            psycopg.connect(TARGET_DEPLOYER_URL) as deployer_conn, psycopg.connect(TARGET_RUNNER_URL) as runner:
        # F1 pre-flight: before the converter (and so Gemini) exists. Any problem stops the run here.
        sequences = sorted(o.name.lower() for o in objects if o.type == ObjectType.SEQUENCE)
        problems = preflight(deployer_conn, runner, database=expected_db, tables=tables, sequences=sequences)
        if args.gemini:
            problem = redis_problem(settings.REDIS_URL)
            if problem:
                problems.append(problem)
        if problems:
            print("Pre-flight: the target is not ready for the real converter. Nothing was converted and Gemini "
                  "was not called.")
            for problem in problems:
                print(f"  - {problem}")
            return EXIT_NOT_RUN
        print(f"Pre-flight: OK (database {expected_db}, {len(tables)} tables, {len(sequences)} sequences, "
              f"roles, {COMPAT_SCHEMA} helper" + (", Redis)" if args.gemini else ")"))

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

        llm = GeminiFallback() if args.gemini else None
        # The deployer re-checks the connection against the configured name before every deploy.
        converter = OracleToPostgresConverter(PostgresDeployer(deployer_conn, database=expected_db), objects, llm=llm)
        print("Converter: REAL (OracleToPostgresConverter). "
              + (f"Gemini: ON, live calls (model {settings.GEMINI_MODEL}, timeout {settings.GEMINI_TIMEOUT_SECONDS} s)."
                 if llm else "Gemini: OFF (no paid calls)."))
        deps = RepairDeps(converter=converter, validate=validate)
        if args.with_edge_cases:
            print("Cases: golden tests plus each object's generated cases (--with-edge-cases).")
        for obj in targets:
            with _gemini_calls(args.gemini) as calls:   # this object's run only (Aarsh's safeguard B)
                run = run_repair_loop(deps, obj, cases_for(obj.object_id, args.with_edge_cases))
            show(run, None)
            runs.append(run)
            per_run.append((run, list(calls)))

    print("\nSummary (real converter)")
    for run in runs:
        tested = run.last_validation
        score = f"{tested.passed}/{len(tested.tests)} tests" if tested else "not tested"
        note = f"  ({run.attempts[-1].failure_code})" if run.attempts[-1].failure_code else ""
        print(f"  {run.object_id:40} {len(run.attempts)} attempt(s)  {score:12} {OUTCOME_LABEL[run.outcome]}{note}")
    if args.gemini:
        show_calls(per_run)
    print("\nThe target database now holds the real converter's output. "
          "Run scripts/seed_target.py again to reset it.\n")
    if args.json:
        args.json.write_text(json.dumps([r.model_dump(mode="json") for r in runs], indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
        if args.gemini:
            calls_path(args.json).write_text(json.dumps(calls_json(per_run), indent=2), encoding="utf-8")
            print(f"Wrote {calls_path(args.json)}")
    return finish(runs)


def _gemini_calls(gemini: bool):
    """llm_service.recording() when Gemini may be called; otherwise an empty list (no SDK import)."""
    if not gemini:
        return contextlib.nullcontext([])
    from app.services import llm_service

    return llm_service.recording()


if __name__ == "__main__":
    sys.exit(main())
