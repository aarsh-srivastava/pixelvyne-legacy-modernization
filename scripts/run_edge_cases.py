"""Run the generated edge-case tests (step 7c) against live Oracle and the converted PostgreSQL.

Prepare the target with the real converter's output first (as for run_repair.py --converter real):
    python scripts/seed_oracle.py
    python scripts/seed_target.py --schema-only
    python scripts/setup_target_roles.py
    python scripts/run_repair.py all --converter real

Then, from the repo root:
    python scripts/run_edge_cases.py                       # all objects, default seed and budget (150)
    python scripts/run_edge_cases.py top_earners           # one object
    python scripts/run_edge_cases.py --record              # also record Oracle's results into the corpus
    python scripts/run_edge_cases.py --case gen-te-rownum_limit-7bad2152
    python scripts/run_edge_cases.py --from-failure reports/edge_cases/<run>/<case>.json
    python scripts/run_edge_cases.py --replay              # no Oracle: PostgreSQL against the recorded corpus

Every case runs in its own rolled-back transaction on each engine, as pv_runner on PostgreSQL;
nothing is committed. --record runs Oracle twice per case and refuses to write the corpus if
any Oracle result is a harness error or differs between the two runs, checked for every case
on its own, whatever its triage class (the reference must be stable). The corpus header then
carries a checksum of the recorded results, so a later edit is refused at replay.
Gemini is never called. A failure record (JSON) is written for every case that does
not pass, under reports/edge_cases/<UTC time>/.

Exit codes: 0 every case passed and every enforced relation held; 1 something did not pass
(the summary gives the class of each); 2 nothing was run (bad arguments, no corpus, ...).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts import ObjectType, TestCase  # noqa: E402
from app.contracts.models import ExecutionError, ExecutionResult  # noqa: E402
from app.parsers.oracle_parser import parse_sources  # noqa: E402
from app.validation import edge_corpus  # noqa: E402
from app.validation.comparator import compare  # noqa: E402
from app.validation.generators import DEFAULT_BUDGET, DEFAULT_SEED, MAX_BUDGET, generate  # noqa: E402
from app.validation.golden_tests import MASKED_COLUMNS  # noqa: E402
from app.validation.relations import evaluate  # noqa: E402
from app.validation.triage import classify, routing_reasons, unstable_cases  # noqa: E402

ORACLE_USER = os.getenv("ORACLE_USER", "legacy")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "legacy")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")
TARGET_RUNNER_URL = os.getenv("TARGET_RUNNER_URL", "postgresql://pv_runner:pv_runner@localhost:5433/target")
ORACLE_CALL_TIMEOUT_MS = 30_000          # PostgreSQL: pv_runner's statement_timeout (30 s)
SAMPLES = ROOT / "samples" / "oracle"
EXIT_PASSED, EXIT_NOT_PASSED, EXIT_NOT_RUN = 0, 1, 2


def guarded(engine: str, runner, conn, obj, case, tables, objects) -> ExecutionResult:
    try:
        return runner(conn, obj, case, tables, objects)
    except Exception as exc:  # noqa: BLE001 - anything here means "could not judge"
        return ExecutionResult(engine=engine, test_id=case.test_id,
                               error=ExecutionError(code="HARNESS", message=f"{type(exc).__name__}: {exc}"))


def deployed_code(pg, obj) -> str | None:
    """SHA-256 (16 hex) of the object's code as deployed in PostgreSQL, or None if it isn't there."""
    name = obj.name.lower()
    if obj.type in (ObjectType.FUNCTION, ObjectType.PROCEDURE):
        schema = (obj.package or "public").lower()
        rows = pg.execute("SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
                          "JOIN pg_namespace n ON n.oid = p.pronamespace "
                          "WHERE n.nspname = %s AND p.proname = %s ORDER BY 1", (schema, name)).fetchall()
    elif obj.type == ObjectType.VIEW:
        rows = pg.execute("SELECT definition FROM pg_views WHERE schemaname = 'public' AND viewname = %s",
                          (name,)).fetchall()
    elif obj.type == ObjectType.TRIGGER:
        rows = pg.execute("SELECT pg_get_triggerdef(t.oid) || pg_get_functiondef(t.tgfoid) FROM pg_trigger t "
                          "WHERE NOT t.tgisinternal AND t.tgname = %s", (name,)).fetchall()
    else:
        rows = []
    pg.rollback()
    if not rows:
        return None
    return hashlib.sha256("\n".join(r[0] for r in rows).encode()).hexdigest()[:16]


def _json(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value)


def write_failure(out_dir: Path, case: TestCase, outcome, triage, extra: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {"case": edge_corpus._encode(case.model_dump()), "class": triage.failure_class.value,
              "reason": triage.reason,
              "severity": triage.severity, "in_contract": triage.in_contract,
              "oracle": outcome.source.model_dump() if outcome.source else None,
              "postgres": outcome.target.model_dump() if outcome.target else None,
              "diffs": [d.model_dump() for d in outcome.diffs], **extra}
    path = out_dir / f"{case.test_id}.json"
    path.write_text(json.dumps(record, indent=1, default=_json), encoding="utf-8")
    return path


def case_from_failure(path: Path) -> TestCase:
    """The exact case in a failure record (values keep their types: Decimal, datetime)."""
    record = json.loads(path.read_text(encoding="utf-8"))
    return TestCase.model_validate(edge_corpus._decode(record["case"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("object", nargs="?", default="all", help="object name, package name, or 'all'")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help=f"cases to generate (max {MAX_BUDGET})")
    parser.add_argument("--record", action="store_true", help="record Oracle's results into the corpus")
    parser.add_argument("--replay", action="store_true", help="no Oracle: compare PostgreSQL with the corpus")
    parser.add_argument("--corpus", type=Path, default=edge_corpus.DEFAULT_CORPUS)
    parser.add_argument("--case", help="run one generated case by ID")
    parser.add_argument("--from-failure", type=Path, help="rerun the case in a failure record")
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "edge_cases")
    args = parser.parse_args(argv)
    if args.record and args.replay:
        print("--record and --replay can't be combined. Nothing was run.")
        return EXIT_NOT_RUN

    sources = {p.name: p.read_text() for p in sorted(SAMPLES.glob("*.sql"))}
    objects = parse_sources(sources)
    by_id = {o.object_id: o for o in objects}
    tables = sorted(o.name.lower() for o in objects if o.type == ObjectType.TABLE)

    try:
        all_cases = generate(args.seed, args.budget)
    except ValueError as exc:
        print(f"{exc}. Nothing was run.")
        return EXIT_NOT_RUN
    if args.from_failure:
        cases = [case_from_failure(args.from_failure)]
    elif args.case:
        cases = [c for c in all_cases if c.test_id == args.case]
    else:
        wanted = args.object.lower()
        cases = [c for c in all_cases if wanted == "all" or by_id[c.object_id].name.lower() == wanted
                 or (by_id[c.object_id].package or "").lower() == wanted]
    if not cases:
        print(f"No generated case matches {args.case or args.object!r} (seed {args.seed}, budget {args.budget}). "
              "Nothing was run.")
        return EXIT_NOT_RUN
    if args.record and len(cases) != len(all_cases):
        print("--record records the whole generated set; don't name an object or a case. Nothing was run.")
        return EXIT_NOT_RUN

    corpus = stale = None
    if args.replay:
        if not args.corpus.exists():
            print(f"No corpus at {args.corpus}. Record one on Oracle first. Nothing was run.")
            return EXIT_NOT_RUN
        corpus = edge_corpus.load(args.corpus)
        stale = corpus.stale_reason(edge_corpus.expected_header(all_cases, args.seed, args.budget, SAMPLES))

    import psycopg

    from app.validation.oracle_runner import run_oracle
    from app.validation.postgres_runner import run_postgres

    ora = oracle_version = None
    if not args.replay:
        import oracledb
        ora = oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)
        ora.call_timeout = ORACLE_CALL_TIMEOUT_MS
        oracle_version = ora.version
    pg = psycopg.connect(TARGET_RUNNER_URL)
    routing = routing_reasons(objects)
    code = {oid: deployed_code(pg, by_id[oid]) for oid in sorted({c.object_id for c in cases})}
    run_dir = args.out / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    oracle_results, pg_results, oracle_twice, triaged, outcomes = {}, {}, {}, [], {}
    try:
        for case in cases:
            obj = by_id[case.object_id]
            if args.replay:
                src = corpus.results.get(case.test_id) if corpus and not stale else None
                if src is None and not stale:
                    stale = f"case {case.test_id} is not in the corpus"
                src = src or ExecutionResult(engine="oracle", test_id=case.test_id)
            else:
                src = guarded("oracle", run_oracle, ora, obj, case, tables, by_id)
                if args.record:
                    oracle_twice[case.test_id] = guarded("oracle", run_oracle, ora, obj, case, tables, by_id)
            tgt = guarded("postgres", run_postgres, pg, obj, case, tables, by_id)
            outcome = compare(case, src, tgt, ordered_rows=bool(case.order_by), masked_columns=MASKED_COLUMNS)
            oracle_results[case.test_id], pg_results[case.test_id], outcomes[case.test_id] = src, tgt, outcome
            result = classify(case, outcome, object_name=obj.name, routing=routing,
                              deployed=code[case.object_id] is not None, stale_reference=stale,
                              oracle_again=oracle_twice.get(case.test_id))
            if result:
                triaged.append(result)
                write_failure(run_dir, case, outcome, result, {
                    "seed": args.seed, "budget": args.budget, "postgres_code_sha": code[case.object_id],
                    "oracle_version": oracle_version or (corpus.header.get("oracle_version") if corpus else None),
                    "corpus_header": corpus.header if corpus else None})
    finally:
        pg.close()
        if ora is not None:
            ora.close()

    relations = [] if stale else evaluate(cases, oracle_results, pg_results)
    report(cases, triaged, relations, by_id, run_dir)

    if args.record:
        # Every case on its own, whatever triage called it (Aarsh's review of revision 1).
        unstable = unstable_cases(cases, oracle_results, oracle_twice)
        oracle_harness = sorted(cid for cid, r in oracle_results.items() if r.error and r.error.code == "HARNESS")
        if unstable or oracle_harness:
            print(f"Corpus NOT written: {len(unstable)} case(s) differ between the two Oracle recordings "
                  f"{unstable}, {len(oracle_harness)} Oracle harness error(s) {oracle_harness}. Fix those first.")
            return EXIT_NOT_PASSED
        header = edge_corpus.expected_header(all_cases, args.seed, args.budget, SAMPLES)
        header.update({"oracle_version": oracle_version,
                       "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        edge_corpus.save(args.corpus, header, oracle_results)
        print(f"Corpus written: {args.corpus} ({len(oracle_results)} Oracle results, recorded twice and stable).")

    bad_relations = [r for r in relations if r.defect]
    if not triaged and not bad_relations:
        print(f"Exit 0 (PASSED): all {len(cases)} generated case(s) passed; every enforced relation held.")
        return EXIT_PASSED
    print(f"Exit 1 (NOT PASSED): {len(triaged)} case(s) and {len(bad_relations)} relation(s) did not pass; "
          f"failure records in {run_dir}.")
    return EXIT_NOT_PASSED


def report(cases, triaged, relations, by_id, run_dir) -> None:
    per_object = Counter(by_id[c.object_id].name for c in cases)
    failing = Counter((by_id[t.object_id].name, t.failure_class.value) for t in triaged)
    print(f"\nGenerated cases: {len(cases)}")
    print(f"{'object':22} {'cases':>5}  not passed (class: count)")
    for name in sorted(per_object):
        classes = ", ".join(f"{cls}: {n}" for (obj, cls), n in sorted(failing.items()) if obj == name)
        print(f"{name:22} {per_object[name]:>5}  {classes or '-'}")
    by_class = Counter(t.failure_class.value for t in triaged)
    print("\nBy class: " + (", ".join(f"{k} {v}" for k, v in sorted(by_class.items())) or "none"))
    for t in triaged:
        tag = "" if t.in_contract else "  [out of contract]"
        print(f"  {t.test_id:42} {t.failure_class.value:18}{tag}\n      {t.reason[:300]}")
    if relations:
        print("\nMetamorphic relations (enforced on PostgreSQL only where Oracle holds):")
        for r in relations:
            pg = r.postgres.value if r.postgres else "-"
            mark = "DEFECT" if r.defect else ("not enforced" if not r.enforced else "ok")
            print(f"  {r.group:18} oracle {r.oracle.value:15} postgres {pg:15} {mark}")
            if r.defect or not r.enforced:
                print(f"      {r.statement}. Oracle: {r.oracle_detail}. PostgreSQL: {r.postgres_detail}")
    print()


if __name__ == "__main__":
    sys.exit(main())
