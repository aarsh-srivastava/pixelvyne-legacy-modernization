"""Stage boundaries. The workflow depends on these protocols, not on implementations,
so a fake converter can stand in while the real one is being built."""

from __future__ import annotations

from typing import Protocol

from app.contracts.models import ConversionResult, FailureReport, IRObject


class Analyzer(Protocol):
    """Parses a project's uploaded files into IR objects, in dependency order."""

    def analyse(self, project_id: str) -> list[IRObject]: ...


class Converter(Protocol):
    """Converts one object. With feedback, it is a repair attempt:
    the failing test diffs must be used to produce a corrected version."""

    def convert(self, obj: IRObject, feedback: FailureReport | None = None) -> ConversionResult: ...
