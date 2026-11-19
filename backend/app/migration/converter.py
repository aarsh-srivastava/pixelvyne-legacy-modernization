"""The Oracle to PostgreSQL converter: the deterministic path (Stage 2, step 5) and the
optional Gemini route for first attempts (step 6b-2) and repairs (step 7a).

Implements the contract ``Converter.convert(obj, feedback=None, *, context=None, run_id=None)``,
by connecting the pieces built in steps 1 to 4 and 6a. No new translation rules live here.

For each call:

1. **Version.** One counter per object: 1, 2, 3... (the repair loop calls ``convert``
   once per attempt and passes ``conversion.version`` to validation).
2. **Feature detection** (step 2). ``REVIEW`` findings stop at once: ``compiled_ok=False``,
   ``needs_review=True``, reason "Unsupported: ..." (decision D5). Nothing is deployed.
3. **Repairs** (``feedback`` and/or ``context`` given). The deterministic rules return the
   same code for the same source, so a deterministic object has no repair route: the
   attempt fails with ``failure_code="no_repair_route"``, without touching the database.
   An object on the Gemini route is repaired by Gemini, but only with a ``RepairContext``
   (D-6.7) describing an attempt this converter produced *in the same run*: every attempt
   is recorded with the ``run_id`` it was made for, and a context from another run, or for
   an attempt made without a run id, is refused. Without a valid context nothing is sent
   to Gemini.
4. **LLM-only constructs** (no rule yet, e.g. ``CONNECT BY``): without ``llm`` they are
   not converted, as before. With ``llm`` they take the Gemini route below.
5. Anything the translators report as unsupported: ``compiled_ok=False`` with the reason;
   nothing is deployed. Translator gaps carry no codes yet, so they never reach Gemini.
6. **Translate** (steps 3 and 4) and **deploy** (step 1). ``compiled_ok`` is the deployer's
   answer: True only after PostgreSQL committed the object.
7. **Unresolved semantics** (PR 1, Aarsh 19 Nov): each use of a function in
   ``allow_list.UNRESOLVED`` (GREATEST) adds a "Needs review: semantic risk ..." warning
   with the object, line and difference, and sets ``needs_review``. Routing is unchanged.
   On both routes the warning is computed before the deploy, and that deploy goes only to the
   validation target (the restricted ``pv_deployer`` connection), never to production.
   There is no production deployment step yet; when one is built it must refuse any object
   whose result has ``needs_review`` until a human sign-off is recorded. The warning and
   the report do not record or enforce sign-off themselves.

**The Gemini route** (only when ``llm`` is given; first attempts since 6b-2, repairs since 7a):

* The object must pass 6a's ``eligibility()`` (REVIEW first, then only listed features),
  checked here before anything is built and again inside ``GeminiFallback.ask``.
* The ``DeployTarget`` comes from the IR alone, named exactly as the translator would.
* The request holds the object, its LLM-only findings, the signatures of the objects it
  uses and the column types of its tables. No deterministic partial code is sent.
* Gemini's SQL reaches the database only through the injected restricted deployer, after
  6a's strict parsing and ``plan_deployment`` checks; the deployer runs those checks again.
* A repair also sends the previous attempt, its deploy error and the last test run (6a's
  ``RepairEvidence``); Gemini returning the same code again is refused (unchanged output).
* Every result on this route has ``tool=GEMINI`` and ``needs_review=True``. ``model`` is
  ``settings.GEMINI_MODEL`` whenever Gemini was asked (never a name from Gemini's answer),
  and ``None`` when it was not. Every failure, deployer or security rejection included,
  is a failed Gemini result needing human review, never an ordinary retryable failure.
* ``compiled_ok=True`` means only that PostgreSQL accepted and committed the object; it
  does not mean the object behaves like Oracle's, and it is not approval.

**Failure codes** (``ConversionResult.failure_code``, contract 0.4) tell the repair loop why
an attempt failed: ``review_required``, ``not_converted``, ``needs_gemini``,
``translator_gap``, ``no_repair_route``, ``deploy_failed``, ``deploy_refused`` and
``converter_error`` on the deterministic path; ``gemini.<code>`` on the Gemini route, with
6a's ``FallbackFailure`` values plus ``repair_unavailable``, ``deploy_failed``,
``deploy_refused`` and ``converter_error``.

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

from app.config import settings
from app.contracts.models import (
    ConversionResult,
    ConversionTool,
    FailureReport,
    IRObject,
    ObjectType,
    RepairContext,
)
from app.contracts.object_ids import parse_object_id
from app.migration.features import (
    FeatureReport,
    Handling,
    analyze_package,
    detect_features,
    known_names,
    semantic_risks,
)
from app.migration.llm_fallback import (
    PROMPT_VERSION,
    ConversionRequest,
    FallbackFailure,
    FallbackOutcome,
    GeminiFallback,
    InvalidRepairEvidence,
    RepairEvidence,
    eligibility,
)
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
#: Objects whose signatures are useful to Gemini; tables are described by column types.
SIGNATURE_KINDS = (ObjectType.FUNCTION, ObjectType.PROCEDURE, ObjectType.SEQUENCE, ObjectType.VIEW)


@dataclass(frozen=True)
class _Translation:
    code: str
    target: DeployTarget | None
    warnings: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()


@dataclass
class OracleToPostgresConverter:
    """Build one per run, with the target deployer and every parsed object.

    ``llm`` is optional: without it the converter is exactly the deterministic step 5
    converter. With a ``GeminiFallback`` it also takes eligible LLM-only objects through
    Gemini: first attempts (step 6b-2) and repairs given a ``RepairContext`` (step 7a).
    """

    deployer: PostgresDeployer
    objects: Iterable[IRObject]
    llm: GeminiFallback | None = None
    _versions: dict[str, int] = field(default_factory=dict, init=False)
    #: (object_id, version) -> (run_id, sha256 of target_code, compiled_ok) for every attempt
    #: this converter produced. Used only to check that a RepairContext describes one of them,
    #: made in the same run. run_id is None for attempts made without one (never repairable).
    _produced: dict[tuple[str, int], tuple[str | None, str, bool]] = field(default_factory=dict, init=False)
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
        self._names = known_names(objects)

    # --- the contract ------------------------------------------------------------------

    def convert(
        self,
        obj: IRObject,
        feedback: FailureReport | None = None,
        *,
        context: RepairContext | None = None,
        run_id: str | None = None,
    ) -> ConversionResult:
        """One attempt for ``obj``. ``run_id`` names the repair-loop run it belongs to. A repair
        (``context`` given) is accepted only with an explicit ``run_id`` equal to the context's,
        so the context can never vouch for the attempt that checks it (D-6.7). Callers that
        never repair may leave ``run_id`` out, as before 7a."""
        version = self._versions.get(obj.object_id, 0) + 1
        self._versions[obj.object_id] = version
        run = run_id  # the attempt's identity comes only from the caller, never from the context
        try:
            result = self._convert(obj, feedback, context, version, run)
        except Exception as exc:  # noqa: BLE001 - one object must never stop the run
            result = self._result(obj, version, error=f"Converter error: {type(exc).__name__}: {exc}",
                                  failure="converter_error")
        self._produced[(obj.object_id, version)] = (run, _sha(result.target_code), result.compiled_ok)
        return result

    # --- the steps ---------------------------------------------------------------------

    def _convert(
        self, obj: IRObject, feedback: FailureReport | None, context: RepairContext | None, version: int,
        run: str | None,
    ) -> ConversionResult:
        if obj.type in (ObjectType.TABLE, ObjectType.SEQUENCE):
            return self._result(obj, version, failure="not_converted", error=(
                "Not converted: tables and sequences are hand-written in phase 1 (D8)."
            ))
        if obj.type == ObjectType.PACKAGE:
            return self._result(obj, version, failure="not_converted", error=(
                f"Not deployed on its own: package members are deployed into schema {obj.name.lower()} (D1)."
            ))

        report = detect_features(obj.source_code, package=obj.package, packages=self._packages,
                                 names=self._names)
        if report.handling is Handling.REVIEW:
            return self._result(obj, version, error=report.review_reason(), needs_review=True,
                                failure="review_required")

        gemini_route = self.llm is not None and report.handling is Handling.LLM

        if feedback is not None or context is not None:
            if gemini_route:
                if context is None:
                    return self._gemini_result(obj, version, failure="repair_unavailable", detail=(
                        "Gemini repairs need a RepairContext from the repair loop (D-6.7); nothing was sent "
                        f"to Gemini. Last failure: {feedback.summary}"
                    ))
                return self._convert_with_gemini(obj, report, version, context, run)
            return self._result(obj, version, failure="no_repair_route", error=(
                f"Repair needs {GEMINI_STEP}: the deterministic rules give the same code for the same "
                f"source. Last failure: {_last_failure(feedback, context)}"
            ))

        if report.handling is Handling.LLM:
            if gemini_route:
                return self._convert_with_gemini(obj, report, version)
            details = "; ".join(f.detail for f in report.by_handling(Handling.LLM))
            return self._result(obj, version, failure="needs_gemini",
                                error=f"No deterministic rule for: {details}. Needs {GEMINI_STEP}.")

        translation = self._translate(obj)
        if translation.unsupported:
            return self._result(obj, version, error="Not converted: " + "; ".join(translation.unsupported),
                                warnings=list(translation.warnings), failure="translator_gap")

        risks = _risk_warnings(obj)  # before the (validation-only) deploy
        deployed = self._deploy(translation)
        warnings = [*translation.warnings, *(f"Needs review: {r}" for r in translation.review_reasons), *risks]
        return self._result(
            obj,
            version,
            code=translation.code,
            compiled_ok=deployed.ok,
            error=None if deployed.ok else deployed.error,
            warnings=warnings,
            needs_review=bool(translation.review_reasons or risks),
            failure=None if deployed.ok else ("deploy_refused" if deployed.refused else "deploy_failed"),
        )

    # --- the Gemini route (step 6b-2) ---------------------------------------------------

    def _convert_with_gemini(
        self, obj: IRObject, report: FeatureReport, version: int, context: RepairContext | None = None,
        run: str | None = None,
    ) -> ConversionResult:
        model = settings.GEMINI_MODEL  # read once per attempt: the warning and the field always agree
        try:
            return self._gemini_attempt(obj, report, version, model, context, run)
        except Exception as exc:  # noqa: BLE001 - still a Gemini-route result needing review, never a crash
            return self._gemini_result(obj, version, failure="converter_error", detail=f"{type(exc).__name__}: {exc}")

    def _gemini_attempt(
        self, obj: IRObject, report: FeatureReport, version: int, model: str, context: RepairContext | None,
        run: str | None = None,
    ) -> ConversionResult:
        not_eligible = FallbackFailure.NOT_ELIGIBLE.value
        gate = eligibility(report)  # checked again inside ask(); nothing is built for a refusal
        if not gate.eligible:
            return self._gemini_result(obj, version, failure=not_eligible, detail=gate.reason)
        target, problem = self._target_for(obj)
        if target is None:
            return self._gemini_result(obj, version, failure=not_eligible, detail=problem)

        repair = None
        if context is not None:  # a repair (step 7a): the evidence must be this run's own attempt
            try:
                repair = self._repair_evidence(obj, context, version, run)
            except InvalidRepairEvidence as exc:
                return self._gemini_result(obj, version, failure=FallbackFailure.INVALID_EVIDENCE.value,
                                           detail=str(exc))

        outcome: FallbackOutcome = self.llm.ask(self._request_for(obj, report, target), report, repair=repair)
        asked = outcome.prompt is not None  # True once a prompt was built and sent (or its send failed)
        if not outcome.ok:
            refused_code = outcome.answer.postgresql if outcome.answer is not None else ""
            return self._gemini_result(obj, version, model=model if asked else None, code=refused_code,
                                       failure=outcome.failure.value, detail=outcome.reason)

        answer = outcome.answer
        risks = _risk_warnings(obj)  # before the (validation-only) deploy, as on the rules route
        deployed = self._deploy(_Translation(answer.postgresql, target))
        warnings = [
            f"Generated by Gemini (model {model}, prompt {PROMPT_VERSION}): human sign-off required",
            f"Repair of v{repair.previous_version}" if repair is not None else "",
            f"Gemini explanation: {answer.explanation.strip()}" if answer.explanation.strip() else "",
            *(f"Gemini warning: {w}" for w in answer.warnings),
            *risks,
        ]
        if deployed.ok:
            return self._gemini_result(obj, version, model=model, code=answer.postgresql, compiled_ok=True,
                                       warnings=warnings)
        return self._gemini_result(obj, version, model=model, code=answer.postgresql, warnings=warnings,
                                   failure="deploy_refused" if deployed.refused else "deploy_failed",
                                   detail=deployed.error)

    def _repair_evidence(
        self, obj: IRObject, context: RepairContext, version: int, run: str | None = None
    ) -> RepairEvidence:
        """6a's RepairEvidence from a RepairContext, after checking it describes this run's own attempt.

        The context must be about ``obj``, about an earlier version, and about an attempt this
        converter actually produced in the same run (same run_id, same code, same deploy
        outcome): evidence from another object, another run or a later attempt is refused,
        never sent to Gemini, even when its code matches.
        """
        previous = context.previous
        if context.object_id != obj.object_id:
            raise InvalidRepairEvidence(f"the repair context is for {context.object_id}, not {obj.object_id}")
        if run is None:
            raise InvalidRepairEvidence("a repair needs the run_id of the attempt being made; none was given")
        if run != context.run_id:
            raise InvalidRepairEvidence("the repair context belongs to a different run than this attempt")
        if previous.version >= version:
            raise InvalidRepairEvidence(
                f"the previous attempt v{previous.version} is not earlier than this attempt v{version}")
        produced = self._produced.get((obj.object_id, previous.version))
        if produced is None or produced[0] != context.run_id:
            raise InvalidRepairEvidence(f"v{previous.version} in the repair context was not produced in this run")
        if produced[1:] != (_sha(previous.code), previous.deployed):
            raise InvalidRepairEvidence(
                f"v{previous.version} in the repair context is not an attempt this converter produced in this run")
        return RepairEvidence(
            object_id=context.object_id,
            previous_version=previous.version,
            previous_code=previous.code,
            deployed=previous.deployed,
            deploy_error=previous.deploy_error,
            last_validation=context.last_validation,
        )

    def _target_for(self, obj: IRObject) -> tuple[DeployTarget | None, str | None]:
        """The object to create, from the IR alone, named exactly as the translator names it."""
        schema = (self._contexts.get(obj.object_id) or ConversionContext()).schema
        name = obj.name.lower()
        try:
            if obj.type == ObjectType.VIEW:
                return DeployTarget("view", name), None
            if obj.type in (ObjectType.FUNCTION, ObjectType.PROCEDURE):
                return DeployTarget(obj.type.value, name, schema=schema), None
            if obj.type == ObjectType.TRIGGER:
                if obj.trigger is None:
                    return None, "the trigger's table is not in the upload (no TriggerInfo)"
                table = parse_object_id(obj.trigger.table).name.lower()
                return DeployTarget("trigger", name, schema=schema, trigger_table=table,
                                    trigger_function=f"{name}_fn"), None
        except ValueError as exc:
            return None, f"no valid PostgreSQL target ({exc})"
        return None, f"{obj.type.value} objects have no Gemini route"

    def _request_for(self, obj: IRObject, report: FeatureReport, target: DeployTarget) -> ConversionRequest:
        """Only IR facts: never settings, environment, connections or deterministic partial code."""
        tables = {t.lower() for t in (*obj.reads_tables, *obj.writes_tables)}
        if obj.trigger is not None:
            tables.add(parse_object_id(obj.trigger.table).name.lower())
        context = self._contexts.get(obj.object_id) or ConversionContext()
        return ConversionRequest(
            obj=obj,
            target=target,
            findings=tuple(f.detail for f in report.by_handling(Handling.LLM)),
            dependencies=tuple(
                self.objects[ref] for ref in obj.depends_on
                if ref in self.objects and self.objects[ref].type in SIGNATURE_KINDS
            ),
            column_types={k: v for k, v in context.column_types.items() if k.split(".", 1)[0] in tables},
        )

    @staticmethod
    def _gemini_result(
        obj: IRObject,
        version: int,
        *,
        failure: str | None = None,
        detail: str | None = None,
        code: str = "",
        compiled_ok: bool = False,
        warnings: list[str] | None = None,
        model: str | None = None,
    ) -> ConversionResult:
        """Every result on the Gemini route: tool GEMINI, always needs review.

        A failure reads ``"Gemini <failure>: <detail>"`` and has ``failure_code`` ``"gemini.<failure>"``.
        ``model`` is the configured setting, passed only when Gemini was asked; never a name
        taken from Gemini's answer.
        """
        return ConversionResult(
            object_id=obj.object_id,
            version=version,
            tool=ConversionTool.GEMINI,
            target_code=code,
            compiled_ok=compiled_ok,
            compile_error=None if compiled_ok else f"Gemini {failure}: {detail}",
            warnings=[w for w in (warnings or []) if w],
            needs_review=True,
            model=model,
            failure_code=None if compiled_ok else f"gemini.{failure}",
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
        """Deploy to the validation target through the restricted deployer, so the tests can
        run. Not a production deployment, and not approval (see step 7 above)."""
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
        failure: str | None = None,
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
            failure_code=None if compiled_ok else failure,
        )


def _risk_warnings(obj: IRObject) -> list[str]:
    """One sign-off warning per use of a function with unresolved semantics (Aarsh, 19 Nov)."""
    return [risk.warning(obj.object_id) for risk in semantic_risks(obj.source_code)]


def _sha(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _last_failure(feedback: FailureReport | None, context: RepairContext | None) -> str:
    if feedback is not None:
        return feedback.summary
    if context is not None and context.previous.deploy_error:
        return f"v{context.previous.version} did not deploy: {context.previous.deploy_error}"
    return "not given"


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
