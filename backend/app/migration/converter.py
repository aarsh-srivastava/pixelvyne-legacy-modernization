"""The Oracle to PostgreSQL converter (Stage 2, step 5): the deterministic path.

Implements the existing contract, ``Converter.convert(obj, feedback) -> ConversionResult``,
by connecting the pieces built in steps 1 to 4. No new translation rules live here.

For each call:

1. **Version.** One counter per object: 1, 2, 3... (the repair loop calls ``convert``
   once per attempt and passes ``conversion.version`` to validation).
2. **Feature detection** (step 2). ``REVIEW`` findings stop at once: ``compiled_ok=False``,
   ``needs_review=True``, reason "Unsupported: ..." (decision D5). Nothing is deployed.
3. **Feedback.** A repair attempt needs a different translation, which only the Gemini
   fallback (step 6) can give: the deterministic rules return the same code for the same
   source. Until then a repair attempt is reported as failed, without touching the
   database, and the repair loop's attempt limit decides what happens next.
4. **LLM-only constructs** (no rule yet, e.g. ``CONNECT BY``) and anything the translators
   report as unsupported: ``compiled_ok=False`` with the reason; nothing is deployed.
5. **Translate** (steps 3 and 4) and **deploy** (step 1). ``compiled_ok`` is the deployer's
   answer: True only after PostgreSQL committed the object.

Never raises: an unexpected error becomes a failed ``ConversionResult``, so one object
can't stop a whole run.

Objects that are not converted on their own, by design:

* Tables and sequences: hand-written in phase 1 (D8).
* Packages: converted through their members, which deploy into a schema named after the
  package (D1). The package object itself has nothing to deploy.

Translations are memoised per object and source text: the deterministic path gives the
same result for the same input, so repeated calls (e.g. re-running a project) don't
translate twice.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.contracts.models import ConversionResult, ConversionTool, FailureReport, IRObject, ObjectType
from app.migration.features import Handling, analyze_package, detect_features
from app.migration.plsql_translator import (
    ConversionContext,
    _name_sets,
    build_contexts,
    translate_plsql,
)
from app.migration.postgres_deployer import DeployResult, DeployTarget, PostgresDeployer
from app.migration.sql_translator import translate_sql

ROUTINES = (ObjectType.FUNCTION, ObjectType.PROCEDURE, ObjectType.TRIGGER)
GEMINI_STEP = "the Gemini fallback (Stage 2, step 6)"


@dataclass(frozen=True)
class _Translation:
    code: str
    target: DeployTarget | None
    warnings: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()


@dataclass
class OracleToPostgresConverter:
    """Deterministic converter. Build one per run, with the target deployer and every parsed object."""

    deployer: PostgresDeployer
    objects: Iterable[IRObject]
    _versions: dict[str, int] = field(default_factory=dict, init=False)
    _cache: dict[tuple[str, str], _Translation] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        objects = list(self.objects)
        self.objects = {o.object_id: o for o in objects}
        self._contexts = build_contexts(objects)
        self._packages = {
            o.name.lower(): analyze_package(o.source_code, [x.source_code for x in objects if x is not o])
            for o in objects
            if o.type == ObjectType.PACKAGE
        }

    # --- the contract ------------------------------------------------------------------

    def convert(self, obj: IRObject, feedback: FailureReport | None = None) -> ConversionResult:
        version = self._versions.get(obj.object_id, 0) + 1
        self._versions[obj.object_id] = version
        try:
            return self._convert(obj, feedback, version)
        except Exception as exc:  # noqa: BLE001 - one object must never stop the run
            return self._result(obj, version, error=f"Converter error: {type(exc).__name__}: {exc}")

    # --- the steps ---------------------------------------------------------------------

    def _convert(self, obj: IRObject, feedback: FailureReport | None, version: int) -> ConversionResult:
        if obj.type in (ObjectType.TABLE, ObjectType.SEQUENCE):
            return self._result(obj, version, error=(
                "Not converted: tables and sequences are hand-written in phase 1 (D8)."
            ))
        if obj.type == ObjectType.PACKAGE:
            return self._result(obj, version, error=(
                f"Not deployed on its own: package members are deployed into schema {obj.name.lower()} (D1)."
            ))

        report = detect_features(obj.source_code, package=obj.package, packages=self._packages)
        if report.handling is Handling.REVIEW:
            return self._result(obj, version, error=report.review_reason(), needs_review=True)

        if feedback is not None:
            return self._result(obj, version, error=(
                f"Repair needs {GEMINI_STEP}: the deterministic rules give the same code for the same "
                f"source. Last failure: {feedback.summary}"
            ))

        if report.handling is Handling.LLM:
            details = "; ".join(f.detail for f in report.by_handling(Handling.LLM))
            return self._result(obj, version, error=f"No deterministic rule for: {details}. Needs {GEMINI_STEP}.")

        translation = self._translate(obj)
        if translation.unsupported:
            return self._result(obj, version, error="Not converted: " + "; ".join(translation.unsupported),
                                warnings=list(translation.warnings))

        deployed = self._deploy(translation)
        warnings = list(translation.warnings) + [f"Needs review: {r}" for r in translation.review_reasons]
        return self._result(
            obj,
            version,
            code=translation.code,
            compiled_ok=deployed.ok,
            error=None if deployed.ok else deployed.error,
            warnings=warnings,
            needs_review=bool(translation.review_reasons),
        )

    def _translate(self, obj: IRObject) -> _Translation:
        key = (obj.object_id, hashlib.sha256(obj.source_code.encode()).hexdigest())
        if key not in self._cache:
            context = self._contexts.get(obj.object_id) or ConversionContext()
            if obj.type in ROUTINES:
                result = translate_plsql(obj.source_code, kind=obj.type.value, name=obj.name, context=context)
                self._cache[key] = _Translation(
                    result.code, result.target, tuple(result.warnings), tuple(result.unsupported),
                    tuple(result.review_reasons),
                )
            elif obj.type == ObjectType.VIEW:
                self._cache[key] = _translate_view(obj, context)
            else:
                self._cache[key] = _Translation("", None, unsupported=(f"{obj.type.value} objects have no rule",))
        return self._cache[key]

    def _deploy(self, translation: _Translation) -> DeployResult:
        try:
            return self.deployer.deploy(translation.code, translation.target)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return DeployResult(ok=False, error=f"Deployer error: {type(exc).__name__}: {exc}")

    @staticmethod
    def _result(
        obj: IRObject,
        version: int,
        *,
        code: str = "",
        compiled_ok: bool = False,
        error: str | None = None,
        warnings: list[str] | None = None,
        needs_review: bool = False,
    ) -> ConversionResult:
        return ConversionResult(
            object_id=obj.object_id,
            version=version,
            tool=ConversionTool.SQLGLOT,
            target_code=code,
            compiled_ok=compiled_ok,
            compile_error=error,
            warnings=warnings or [],
            needs_review=needs_review,
        )


# --- views ------------------------------------------------------------------------------

_VIEW = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:NO)?FORCE\s+)?(?:EDITIONABLE\s+)?VIEW\s+"
    r"(?:[A-Za-z_][\w$#]*\s*\.\s*)?([A-Za-z_][\w$#]*)\s*(\([^)]*\))?\s+AS\s+(.*?)[\s;/]*$",
    re.I | re.S,
)


def _translate_view(obj: IRObject, context: ConversionContext) -> _Translation:
    """CREATE VIEW name [(columns)] AS <query>: the query goes through the SQL rules."""
    match = _VIEW.match(obj.source_code)
    if not match or match.group(1).lower() != obj.name.lower():
        return _Translation("", None, unsupported=("view header has no rule",))
    query = match.group(3)
    if re.search(r"\bWITH\s+(READ\s+ONLY|CHECK\s+OPTION)\b", query, re.I):
        return _Translation("", None, unsupported=("WITH READ ONLY / CHECK OPTION has no rule yet",))
    dates, numbers = _name_sets(context.column_types)
    result = translate_sql(
        query,
        date_names=dates,
        number_names=numbers,
        known_functions=context.known_functions,
        function_renames=context.function_renames,
        constants=context.constants,
    )
    if not result.successful:
        return _Translation("", None, unsupported=tuple(result.unsupported_features))
    columns = f" {match.group(2).lower()}" if match.group(2) else ""
    name = obj.name.lower()
    return _Translation(f"CREATE OR REPLACE VIEW {name}{columns} AS {result.target_sql};\n", DeployTarget("view", name))
