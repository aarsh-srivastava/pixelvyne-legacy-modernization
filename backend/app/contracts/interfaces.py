"""Stage boundaries. The workflow depends on these protocols, not on implementations,
so a fake converter can stand in while the real one is being built."""

from __future__ import annotations

from typing import Protocol

from app.contracts.models import ConversionResult, FailureReport, IRObject, RepairContext


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
