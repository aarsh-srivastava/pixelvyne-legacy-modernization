"""Create (or re-restrict) the two least-privilege roles in the target sandbox.

Usage, from the repo root on your laptop, after seed_target.py:
    python scripts/setup_target_roles.py

pv_deployer creates and owns converted objects; pv_runner executes them during
validation. Neither is a superuser. Run it again after every seed_target.py,
because seeding recreates the public schema and its grants.

Connects as the sandbox admin (TARGET_DATABASE_URL_HOST, default as below). Role
passwords come from PV_DEPLOYER_PASSWORD / PV_RUNNER_PASSWORD; the defaults are
for the local throwaway sandbox only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

ADMIN_URL = os.getenv("TARGET_DATABASE_URL_HOST", "postgresql://sandbox:sandbox@localhost:5433/target")
DEPLOYER_PASSWORD = os.getenv("PV_DEPLOYER_PASSWORD", "pv_deployer")
RUNNER_PASSWORD = os.getenv("PV_RUNNER_PASSWORD", "pv_runner")


def main() -> int:
    import psycopg

    from app.migration.target_roles import DEPLOYER_ROLE, RUNNER_ROLE, ensure_roles, role_report

    with psycopg.connect(ADMIN_URL) as conn:
        db = conn.execute("SELECT current_database()").fetchone()[0]
        if db != "target":
            print(f"Refusing to change roles in database '{db}': expected 'target'.")
            return 1
        ensure_roles(conn, deployer_password=DEPLOYER_PASSWORD, runner_password=RUNNER_PASSWORD)
        for role in (DEPLOYER_ROLE, RUNNER_ROLE):
            facts = role_report(conn, role)
            unsafe = [k for k in ("superuser", "createdb", "createrole", "replication", "bypassrls") if facts[k]]
            if unsafe or facts["member_of"]:
                print(f"{role}: NOT restricted: {unsafe or facts['member_of']}")
                return 1
            print(f"{role}: restricted (no superuser, no createdb/createrole, member of no roles)")
    print("Roles ready. Connect as pv_deployer to deploy and pv_runner to validate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
