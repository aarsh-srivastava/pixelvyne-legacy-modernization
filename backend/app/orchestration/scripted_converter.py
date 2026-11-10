"""A stand-in Converter that replays prepared PostgreSQL versions. NOT the product.

It lets the repair loop run end to end against real databases before Aarsh's
converter exists, and it is replaced by the real one through the same Converter
protocol. It never writes code itself:

* Attempt 1 is whatever seed_target.py deployed (the careless, literal conversion
  in samples/postgres/), read back from the database.
* Each repair attempt applies the next prepared file, e.g.
  samples/postgres/repairs/calc_bonus.sql, and reports whether it compiled.
* With no prepared file left, it re-reports the last version, so the loop runs out
  of attempts and hands the object to a human, as it would with a real converter
  that cannot fix the problem.

Every result is tool=MANUAL with a warning saying it is a stand-in.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from app.contracts import ConversionResult, ConversionTool, FailureReport, IRObject

STAND_IN_WARNING = "Scripted stand-in: prepared PostgreSQL replayed, not converted."

_DEPLOYED_SQL = """
    SELECT pg_get_functiondef(p.oid)
      FROM pg_proc p
     WHERE p.proname = %s AND p.pronamespace = %s::regnamespace
     ORDER BY p.oid
     LIMIT 1
"""


class ScriptedConverter:
    def __init__(self, pg_conn: psycopg.Connection, repairs: dict[str, list[Path]],
                 schema: str = "public"):
        """repairs: object_id -> prepared files for attempts 2, 3, ... in order."""
        self._conn = pg_conn
        self._repairs = repairs
        self._schema = schema
        self._calls: dict[str, int] = {}
        self._last: dict[str, ConversionResult] = {}

    def convert(self, obj: IRObject, feedback: FailureReport | None = None) -> ConversionResult:
        version = self._calls.get(obj.object_id, 0) + 1
        self._calls[obj.object_id] = version

        if version == 1:
            result = self._deployed(obj, version)
        else:
            files = self._repairs.get(obj.object_id, [])
            if version - 2 < len(files):
                result = self._apply(obj, version, files[version - 2])
            else:
                previous = self._last[obj.object_id]
                result = previous.model_copy(update={
                    "version": version,
                    "warnings": [STAND_IN_WARNING, "No prepared repair left; same code as before."],
                })
        self._last[obj.object_id] = result
        return result

    def _deployed(self, obj: IRObject, version: int) -> ConversionResult:
        row = self._conn.execute(_DEPLOYED_SQL, [obj.name.lower(), self._schema]).fetchone()
        self._conn.rollback()
        if row is None:
            return self._result(obj, version, "", ok=False,
                                error=f"{obj.name} is not deployed in the target database. "
                                      "Run scripts/seed_target.py first.")
        return self._result(obj, version, row[0], ok=True)

    def _apply(self, obj: IRObject, version: int, path: Path) -> ConversionResult:
        code = path.read_text(encoding="utf-8")
        try:
            self._conn.execute(code)
            self._conn.commit()
        except psycopg.Error as exc:
            self._conn.rollback()
            first_line = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            return self._result(obj, version, code, ok=False, error=first_line)
        return self._result(obj, version, code, ok=True)

    @staticmethod
    def _result(obj: IRObject, version: int, code: str, *, ok: bool,
                error: str | None = None) -> ConversionResult:
        return ConversionResult(
            object_id=obj.object_id, version=version, tool=ConversionTool.MANUAL,
            target_code=code, compiled_ok=ok, compile_error=error,
            warnings=[STAND_IN_WARNING],
        )
