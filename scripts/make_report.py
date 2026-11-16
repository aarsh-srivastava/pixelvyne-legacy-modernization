"""Build the migration report (one HTML page) from a repair-loop run.

Usage, from the repo root on your laptop:
    python scripts/seed_target.py
    python scripts/run_repair.py all --json reports/repair_runs.json
    python scripts/make_report.py reports/repair_runs.json

Writes reports/migration_report.html next to the input unless -o is given.
The reports/ folder is git-ignored: reports are evidence of a run, not source code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.orchestration.repair_loop import RepairRun  # noqa: E402
from app.reporting.migration_report import ReportSummary, render_report  # noqa: E402
from app.validation.golden_tests import GOLDEN_TESTS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs_json", type=Path, help="file written by run_repair.py --json")
    parser.add_argument("-o", "--output", type=Path, help="HTML file to write")
    args = parser.parse_args()

    if not args.runs_json.exists():
        print(f"{args.runs_json} not found. Run: python scripts/run_repair.py all --json {args.runs_json}")
        return 1
    runs = [RepairRun.model_validate(r) for r in json.loads(args.runs_json.read_text(encoding="utf-8"))]
    descriptions = {c.test_id: c.description for cases in GOLDEN_TESTS.values() for c in cases}

    output = args.output or args.runs_json.with_name("migration_report.html")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_report(runs, descriptions=descriptions), encoding="utf-8")

    s = ReportSummary.of(runs)
    print(f"{s.equivalent + s.needs_review} of {s.objects} objects equivalent, "
          f"{s.tests_same} of {s.tests_total} tests matching Oracle, "
          f"{s.repairs} automatic repairs, {s.needs_human} need a human.")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
