"""Stage boundaries. The workflow depends on these protocols, not on implementations,
so a fake converter can stand in while the real one is being built.

The pipeline protocols (contract 0.7) are the seam between the two workstreams: the
engine (B) implements MigrationEngine and calls a SchemaMigrator and a ProgressSink that
the caller passes in; the platform (A) implements SchemaMigrator, SourceProvider,
ProgressSink and ReportWriter, and calls MigrationEngine.run_migration. Neither side
imports the other's modules. The protocols are runtime-checkable, so a contract test can
check that an implementation (or a fake) has the right methods.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.contracts.models import (
    ConversionResult,
    FailureReport,
    IRObject,
    RepairContext,
)
from app.contracts.pipeline import (
    MigrationOptions,
    MigrationRun,
    SourceFile,
    TargetEnvironment,
)
from app.contracts.schema_defs import SchemaMigrationResult


class Analyzer(Protocol):
    """Parses a project's uploaded files into IR objects, in dependency order."""

    def analyse(self, project_id: str) -> list[IRObject]: ...


class Converter(Protocol):
    """Converts one object. With feedback or context, it is a repair attempt.

    ``feedback`` describes the failing tests of the last test run (if any); ``context``
    (contract 0.4, D-6.7) carries the attempt being repaired and that test run, each tied
    to its version. ``run_id`` (0.4) names the repair-loop run the attempt belongs to; the
    repair loop passes it on every attempt, so a converter can refuse a context from another
    run. Both are keyword-only and optional: ``convert(obj)`` works as before.

    Converters written against the interface before 7a, ``convert(obj, feedback=None)``,
    remain supported: the repair loop passes only the keywords a converter's ``convert``
    accepts. A RepairContext is passed only to a converter accepting BOTH ``context`` and
    ``run_id`` (so it can check the evidence's run); any other converter is called exactly
    as before 7a and gets no repair evidence. Gemini repairs need both keywords.
    """

    def convert(
        self,
        obj: IRObject,
        feedback: FailureReport | None = None,
        *,
        context: RepairContext | None = None,
        run_id: str | None = None,
    ) -> ConversionResult: ...


# ---------------------------------------------------------------- pipeline (contract 0.7)

class Stage(str, Enum):
    """Where a run is, for ProgressSink.update."""

    PARSE = "parse"          # reading the sources into objects
    SCHEMA = "schema"        # SchemaMigrator.migrate
    OBJECTS = "objects"      # converting, deploying, testing and repairing, one object at a time
    DONE = "done"            # the MigrationRun is complete


@runtime_checkable
class ProgressSink(Protocol):
    """Told what the engine is doing (A: job progress). ``0 <= done <= total``; ``object_id``
    is the object being worked on, or None. Must not raise: progress is never a reason to stop."""

    def update(self, stage: Stage, object_id: str | None, done: int, total: int) -> None: ...


@runtime_checkable
class SchemaMigrator(Protocol):
    """Gets the target's tables and sequences ready (A: schema/, M3; stand-in: PreparedTargetSchema).

    ``expected_tables`` and ``expected_sequences`` are the object_ids the engine discovered;
    the result has exactly one outcome for each. It reads the DDL from ``sources`` itself
    and takes no IRObject. Problems are reported in the result, not raised.
    """

    def migrate(
        self,
        sources: list[SourceFile],
        env: TargetEnvironment,
        *,
        expected_tables: list[str],
        expected_sequences: list[str],
    ) -> SchemaMigrationResult: ...


@runtime_checkable
class MigrationEngine(Protocol):
    """Runs every stage for every object (B: orchestration/pipeline.py, M1).

    Calls ``schema.migrate`` first; if its result is not ``ok``, returns a run with status
    not_run and nothing converted. Raises MigrationInputError only for unusable input.
    """

    def run_migration(
        self,
        sources: list[SourceFile],
        options: MigrationOptions,
        env: TargetEnvironment,
        *,
        schema: SchemaMigrator,
        progress: ProgressSink | None = None,
    ) -> MigrationRun: ...


@runtime_checkable
class SourceProvider(Protocol):
    """Hands the engine its Oracle sources (A: upload, live Oracle; B: a folder for the CLI)."""

    def sources(self) -> list[SourceFile]: ...


@runtime_checkable
class ReportWriter(Protocol):
    """Writes a run's report files into ``out_dir`` and returns their paths (A: reporting/)."""

    def write(self, run: MigrationRun, out_dir: Path) -> list[Path]: ...
