"""Assemble a MigrationRun (contract 0.7) from what the engine's stages produced.

The engine's own side of the contract: it knows each object's route and turns repair runs,
schema outcomes and Gemini call records into ObjectOutcomes. ``build_migration_run`` gives
every discovered object exactly one outcome or raises: an object with no repair run, no
schema outcome and no stated reason is a silent skip, which is a bug in the caller.

Used by scripts/build_run_fixture.py today, and by run_migration (M1) next.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Literal

from app.contracts import (
    GeminiCall,
    IRObject,
    MigrationOptions,
    MigrationRun,
    ObjectOutcome,
    ObjectStatus,
    ObjectType,
    RepairRun,
    Route,
    RunMetrics,
    RunStatus,
    SchemaMigrationResult,
    SourceFile,
    status_for_repair,
    status_for_schema,
)
from app.migration.features import Handling, analyze_package, detect_features

_ROUTE_BY_HANDLING = {Handling.RULE: Route.RULES, Handling.LLM: Route.GEMINI, Handling.REVIEW: Route.REVIEW}
#: Why a package is never converted on its own (the converter's D1 decision).
PACKAGE_REASON = "Not deployed on its own: its members are converted, deployed and tested one by one (D1)."


def routes_for(objects: Iterable[IRObject]) -> dict[str, Route]:
    """Each object's route, classified the way the converter classifies it (features.py)."""
    objects = list(objects)
    packages = {
        o.name.lower(): analyze_package(o.source_code, [x.source_code for x in objects if x is not o])
        for o in objects if o.type == ObjectType.PACKAGE
    }
    routes = {}
    for obj in objects:
        if obj.type in (ObjectType.TABLE, ObjectType.SEQUENCE):
            routes[obj.object_id] = Route.SCHEMA
        elif obj.type == ObjectType.PACKAGE:
            routes[obj.object_id] = Route.NONE
        else:
            handling = detect_features(obj.source_code, package=obj.package, packages=packages).handling
            routes[obj.object_id] = _ROUTE_BY_HANDLING[handling]
    return routes


def gemini_calls(records: Iterable) -> list[GeminiCall]:
    """llm_service.CallRecords (dataclasses) as contract GeminiCalls."""
    return [GeminiCall(**asdict(r)) for r in records]


def build_migration_run(
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    engine: str,
    stand_in: bool,
    oracle_answers: Literal["live", "recorded"],
    options: MigrationOptions,
    sources: list[SourceFile],
    objects: list[IRObject],
    schema_result: SchemaMigrationResult,
    repairs: Mapping[str, RepairRun],
    calls: Mapping[str, list[GeminiCall]] | None = None,
    not_converted: Mapping[str, str] | None = None,
    blocked_by: Mapping[str, list[str]] | None = None,
    problems: list[str] | None = None,
    status: RunStatus = RunStatus.COMPLETED,
) -> MigrationRun:
    """One outcome per object, then the run (whose validator checks every promise again).

    ``not_converted`` gives the reason for each object deliberately left alone (packages need
    none: PACKAGE_REASON). When ``status`` is not_run, every routine and view is
    not_converted with the run's problems as the reason.
    """
    calls, not_converted, blocked_by = calls or {}, dict(not_converted or {}), blocked_by or {}
    routes = routes_for(objects)
    outcomes = []
    for obj in objects:
        route, base = routes[obj.object_id], {"object_id": obj.object_id, "type": obj.type,
                                              "source_file": obj.source_file}
        schema = schema_result.outcome(obj.object_id) if route is Route.SCHEMA else None
        repair = repairs.get(obj.object_id)
        if route is Route.SCHEMA:
            if schema is None:
                raise ValueError(f"no schema outcome for {obj.object_id}")
            outcomes.append(ObjectOutcome(**base, route=route, status=status_for_schema(schema), reason=schema.reason))
        elif status is RunStatus.NOT_RUN:
            outcomes.append(ObjectOutcome(**base, route=route, status=ObjectStatus.NOT_CONVERTED,
                                          reason="Not converted: the run did not start. " + "; ".join(problems or [])))
        elif repair is not None:
            outcomes.append(ObjectOutcome(**base, route=route, status=status_for_repair(repair), reason=repair.reason,
                                          category=repair.category, repair=repair,
                                          gemini_calls=calls.get(obj.object_id, [])))
        elif obj.type == ObjectType.PACKAGE or obj.object_id in not_converted:
            outcomes.append(ObjectOutcome(**base, route=route, status=ObjectStatus.NOT_CONVERTED,
                                          reason=not_converted.get(obj.object_id, PACKAGE_REASON),
                                          blocked_by=blocked_by.get(obj.object_id, [])))
        else:
            raise ValueError(f"{obj.object_id} has no repair run, schema outcome or reason: a silent skip")
    return MigrationRun(
        run_id=run_id, status=status, started_at=started_at, finished_at=finished_at, engine=engine,
        stand_in=stand_in, oracle_answers=oracle_answers, options=options, sources=[s.ref() for s in sources],
        discovered=[o.object_id for o in objects], schema_result=schema_result, objects=outcomes,
        problems=list(problems or []), metrics=RunMetrics.of(outcomes, schema_result, len(objects)),
    )
