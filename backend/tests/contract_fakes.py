"""Shared stand-ins for the pipeline contracts (0.7), so each workstream builds without the other.

* ``FakeMigrationEngine`` (for A: reports, storage, API, worker): returns the sample
  MigrationRun from tests/fixtures/migration_run_sample.json, marked as a stand-in, and
  reports progress like the real engine will.
* ``FakeSchemaMigrator`` (for B: the engine before M3, without a database): reports every
  expected table and sequence as present, or as missing when asked.

The real stand-in against PostgreSQL is app.migration.prepared_target.PreparedTargetSchema.
test_contract_v07.py checks that all of them satisfy the protocols.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts import (
    ColumnDef,
    MigrationInputError,
    MigrationOptions,
    MigrationRun,
    ProgressSink,
    SchemaMigrationResult,
    SchemaMigrator,
    SchemaObjectOutcome,
    SchemaObjectStatus,
    SequenceDef,
    SourceFile,
    Stage,
    TableDef,
    TargetEnvironment,
    parse_object_id,
)

SAMPLE_RUN = Path(__file__).resolve().parent / "fixtures" / "migration_run_sample.json"


def load_sample_run() -> MigrationRun:
    return MigrationRun.model_validate(json.loads(SAMPLE_RUN.read_text(encoding="utf-8")))


class FakeMigrationEngine:
    """A MigrationEngine that returns a recorded run (default: the sample fixture)."""

    def __init__(self, run: MigrationRun | None = None):
        self.run = (run or load_sample_run()).model_copy(
            update={"stand_in": True, "engine": "FakeMigrationEngine (recorded run)"})
        self.calls: list[tuple[list[SourceFile], MigrationOptions]] = []

    def run_migration(
        self,
        sources: list[SourceFile],
        options: MigrationOptions,
        env: TargetEnvironment,
        *,
        schema: SchemaMigrator,
        progress: ProgressSink | None = None,
    ) -> MigrationRun:
        if not sources:
            raise MigrationInputError("no sources")
        names = [s.name for s in sources]
        if len(set(names)) != len(names):
            raise MigrationInputError("source names must be unique")
        self.calls.append((sources, options))
        objects = [o.object_id for o in self.run.objects]
        if progress is not None:
            progress.update(Stage.PARSE, None, 0, len(objects))
            progress.update(Stage.SCHEMA, None, 0, len(objects))
            for done, object_id in enumerate(objects, start=1):
                progress.update(Stage.OBJECTS, object_id, done, len(objects))
            progress.update(Stage.DONE, None, len(objects), len(objects))
        return self.run


class FakeSchemaMigrator:
    """A SchemaMigrator for tests: every expected object present, except those named in ``missing``."""

    name = "FakeSchemaMigrator"

    def __init__(self, missing: set[str] | None = None, problems: list[str] | None = None):
        self.missing, self.problems = set(missing or ()), list(problems or [])
        self.calls: list[tuple[list[str], list[str]]] = []

    def migrate(
        self,
        sources: list[SourceFile],
        env: TargetEnvironment,
        *,
        expected_tables: list[str],
        expected_sequences: list[str],
    ) -> SchemaMigrationResult:
        self.calls.append((list(expected_tables), list(expected_sequences)))
        outcomes = []
        for object_id in list(expected_tables) + list(expected_sequences):
            name = parse_object_id(object_id).name
            if object_id in self.missing:
                outcomes.append(SchemaObjectOutcome(object_id=object_id, status=SchemaObjectStatus.MISSING,
                                                    reason="missing (FakeSchemaMigrator)"))
                continue
            if parse_object_id(object_id).kind == "table":
                definition = TableDef(object_id=object_id, target_name=name,
                                      columns=[ColumnDef(name="id", oracle_type="NUMBER", pg_type="numeric")])
            else:
                definition = SequenceDef(object_id=object_id, target_name=name)
            outcomes.append(SchemaObjectOutcome(object_id=object_id, status=SchemaObjectStatus.PRESENT,
                                                reason="present (FakeSchemaMigrator)", definition=definition))
        return SchemaMigrationResult(mode="prepared", migrator=self.name, database=env.database_name,
                                     objects=outcomes, problems=self.problems)


class RecordingProgress:
    """A ProgressSink that keeps every update."""

    def __init__(self):
        self.updates: list[tuple[Stage, str | None, int, int]] = []

    def update(self, stage: Stage, object_id: str | None, done: int, total: int) -> None:
        self.updates.append((stage, object_id, done, total))
