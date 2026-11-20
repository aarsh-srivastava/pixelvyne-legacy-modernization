"""Internal contracts between the pipeline stages.

Keeping stages talking only through these models lets each one be built, faked and
tested on its own. Bump CONTRACT_VERSION whenever a field changes.

Flow of data:
    analysis    -> IRObject
    conversion  -> ConversionResult
    validation  -> TestCase, ExecutionResult, TestOutcome, ValidationResult
    repair      -> FailureReport, fed back into convert(obj, feedback=...)
                -> RepairRun (one per object: every AttemptRecord, the outcome and why)
    pipeline    -> MigrationRun (pipeline.py): every source object's final status (0.7)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.contracts.object_ids import normalize_identifier, parse_object_id

# 0.7: pipeline contracts (pipeline.py, schema_defs.py); RepairRun, AttemptRecord, RepairOutcome
#      and FailureCategory moved here from orchestration/repair_loop.py (still re-exported there);
# 0.6: TestCase.generation, ExecutionError.context; 0.5: TestCase.setup/action/order_by;
# 0.4: RepairContext, failure_code; 0.3: .model
CONTRACT_VERSION = "0.7"


# ---------------------------------------------------------------- enums

class ObjectType(str, Enum):
    TABLE = "table"
    VIEW = "view"
    SEQUENCE = "sequence"
    FUNCTION = "function"
    PROCEDURE = "procedure"
    TRIGGER = "trigger"
    PACKAGE = "package"


class ParamMode(str, Enum):
    IN = "in"
    OUT = "out"
    IN_OUT = "in_out"


class ConversionTool(str, Enum):
    ORA2PG = "ora2pg"
    SQLGLOT = "sqlglot"
    GEMINI = "gemini"
    MANUAL = "manual"


class TestStatus(str, Enum):
    __test__ = False

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"          # harness itself broke (not a behaviour difference)


class ValidationStatus(str, Enum):
    PASSED = "passed"
    REQUIRES_REPAIR = "requires_repair"
    NEEDS_HUMAN = "needs_human"


# ---------------------------------------------------------------- analysis + conversion

class Param(BaseModel):
    name: str                          # lower-case, e.g. "p_emp_id"
    oracle_type: str                   # as declared, e.g. "NUMBER", "VARCHAR2", "INTEGER", "DATE"
    mode: ParamMode = ParamMode.IN
    has_default: bool = False
    default_expr: str | None = None    # e.g. "SYSDATE"


class TriggerInfo(BaseModel):
    """What makes a trigger fire. What it changes is separate: IRObject.writes_tables.

    For  AFTER UPDATE OF base_salary ON employees FOR EACH ROW ... INSERT INTO salary_audit
        table="table:legacy.employees", timing="after", events=["update"],
        update_columns=["base_salary"], for_each_row=True
    and writes_tables=["salary_audit"]. The fired-on table is also in depends_on.

    If the fired-on table is not in the upload, the trigger gets no TriggerInfo
    (trigger=None); the reference stays in depends_on as "unresolved:...", which the
    planner turns into a missing_dependency blocker.
    """

    table: str                         # canonical table or view object_id, never unresolved
    timing: Literal["before", "after", "instead_of"]
    events: list[Literal["insert", "update", "delete"]]
    update_columns: list[str] = Field(default_factory=list)   # UPDATE OF ..., normalized
    for_each_row: bool = False         # False = statement-level trigger

    @field_validator("table")
    @classmethod
    def _table_is_canonical(cls, value: str) -> str:
        if parse_object_id(value).kind not in {"table", "view"}:
            raise ValueError(f"Trigger target must be a table or view object_id, got {value!r}")
        return value

    @field_validator("events")
    @classmethod
    def _events_not_empty(cls, value: list[str]) -> list[str]:
        if not value or len(set(value)) != len(value):
            raise ValueError("events must list each triggering DML event once")
        return value

    @field_validator("update_columns")
    @classmethod
    def _columns_normalized(cls, value: list[str]) -> list[str]:
        return [normalize_identifier(c) for c in value]


class IRObject(BaseModel):
    """One source object, as understood by the analysis step."""

    object_id: str                     # canonical, see object_ids.py: "procedure:legacy.calc_bonus"
    name: str                          # lower-case, unqualified
    type: ObjectType
    package: str | None = None         # set for members of a package
    params: list[Param] = Field(default_factory=list)
    return_type: str | None = None     # functions only, Oracle type
    reads_tables: list[str] = Field(default_factory=list)
    writes_tables: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)   # canonical object_ids, incl. "unresolved:..."
    business_rules: list[str] = Field(default_factory=list)
    source_code: str
    source_file: str | None = None
    oracle_features: list[str] = Field(default_factory=list)  # e.g. ["DECODE", "ROWNUM"]
    trigger: TriggerInfo | None = None # triggers only


class ConversionResult(BaseModel):
    """One conversion attempt for one object.

    * ``compiled_ok=True`` means only that PostgreSQL accepted and committed the object
      through the restricted deployer. It does NOT mean the object behaves like the Oracle
      original, and it is not final approval: behaviour is established only by the golden
      tests in the repair loop.
    * ``needs_review=True`` means a human must still approve the object, whatever the
      tests say. It is always set on results generated by Gemini.
    * ``model`` is the LLM that produced ``target_code`` (from settings, never from the
      model's own answer); ``None`` when no LLM was involved. Added in contract 0.3; older
      results without it load as ``None``.
    * ``failure_code`` says, machine-readably, why a failed attempt failed, so the repair
      loop can decide whether another attempt can help (e.g. ``"review_required"``,
      ``"gemini.deploy_failed"``). ``None`` on success and from converters that do not set
      it (the loop then treats a failure as retryable, as before). Added in contract 0.4.
    """

    object_id: str
    version: int                       # 1, 2, 3... one per attempt, never overwritten
    tool: ConversionTool
    target_code: str
    compiled_ok: bool                  # deployed by PostgreSQL; NOT behavioural equivalence
    compile_error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    needs_review: bool = False         # a human must still approve
    model: str | None = None           # LLM that produced target_code, e.g. "gemini-2.5-flash"
    failure_code: str | None = None    # why a failed attempt failed; None on success (0.4)
    produced_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------- validation

_PLAIN_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class SetupCall(BaseModel):
    """A routine run before the test, in the same rolled-back transaction (contract 0.5).

    Its result is never compared; only what the test itself then does is. E.g. pk-04 runs
    ``monthly_payroll(2026-09-15)`` first, then tests ``close_month``.
    """

    object_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)          # IN params by name


class GenerationInfo(BaseModel):
    """How a generated test case was made (contract 0.6, step 7c). Golden tests have none.

    Enough to regenerate the case exactly: the generator version and seed, plus what it is
    for. ``in_contract`` is False when the inputs are outside what EXPECTED.md promises
    (e.g. a fractional percentage); such cases are still compared against Oracle, but their
    differences are reported separately and are not fixed without a separate decision.
    """

    generator_version: str
    seed: int
    category: str                      # e.g. "null_empty", "month_end", "rownum_limit"
    rule: str | None = None            # translator rule targeted, e.g. "R9"
    in_contract: bool = True
    relation: str | None = None        # metamorphic relation group this case belongs to


class TestCase(BaseModel):
    """One behavioural test, run on both engines and compared.

    How the object is exercised depends on its type: a function or procedure is called
    with ``inputs``; a view is queried (``SELECT * ... ORDER BY order_by``) and its rows
    compared; a trigger is fired by ``action``, one fixed DML statement. ``setup`` (and
    the older ``setup_calls``) run first. Fields added in 0.5 are optional, so test cases
    written for 0.2 to 0.4 load unchanged.
    """

    __test__ = False  # not a pytest class

    test_id: str                       # e.g. "cb-02"
    object_id: str
    description: str
    inputs: dict[str, Any] = Field(default_factory=dict)          # IN params by name
    fixture_rows: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)  # table -> rows to insert first
    setup_calls: list[str] = Field(default_factory=list)          # object_ids to run first, no inputs (0.2)
    frozen_now: datetime | None = None                            # replaces SYSDATE / now()
    origin: Literal["golden", "rule", "llm"] = "rule"
    rationale: str | None = None
    setup: list[SetupCall] = Field(default_factory=list)          # (0.5) calls with inputs, run first
    action: str | None = None          # (0.5) one INSERT/UPDATE/DELETE that fires a trigger; trusted tests only
    order_by: list[str] = Field(default_factory=list)            # (0.5) view tests: rows compared in this order
    generation: GenerationInfo | None = None                    # (0.6) generated cases only (step 7c)

    @field_validator("order_by")
    @classmethod
    def _plain_columns(cls, value: list[str]) -> list[str]:
        for column in value:
            if not _PLAIN_COLUMN.match(column):
                raise ValueError(f"order_by takes plain column names, got {column!r}")
        return [c.lower() for c in value]

    def setup_steps(self) -> list[SetupCall]:
        """Everything to run before the test: ``setup_calls`` (no inputs) first, then ``setup``."""
        return [SetupCall(object_id=object_id) for object_id in self.setup_calls] + list(self.setup)


class ExecutionError(BaseModel):
    code: str | None = None            # e.g. "ORA-01403", "P0002"
    message: str
    # (0.6) where it was raised: Oracle's ORA-06512 lines, PostgreSQL's PL/pgSQL context.
    # Evidence for triage (raised inside the object or at the call); never compared.
    context: str | None = None


class TableDiff(BaseModel):
    table: str
    inserted: list[dict[str, Any]] = Field(default_factory=list)
    deleted: list[dict[str, Any]] = Field(default_factory=list)
    updated: list[dict[str, Any]] = Field(default_factory=list)   # {"key": ..., "before": {...}, "after": {...}}


class ExecutionResult(BaseModel):
    engine: Literal["oracle", "postgres"]
    test_id: str
    return_value: Any = None
    out_params: dict[str, Any] = Field(default_factory=dict)
    rows: list[dict[str, Any]] | None = None       # for views / row-returning objects
    table_diffs: list[TableDiff] = Field(default_factory=list)
    error: ExecutionError | None = None
    duration_ms: float = 0.0


class FieldDiff(BaseModel):
    path: str                          # e.g. "out_params.p_bonus", "table_diffs.payroll_runs[emp_id=108].gross_pay"
    source_value: Any = None
    target_value: Any = None
    kind: Literal["value", "type", "missing", "extra", "error"] = "value"


class TestOutcome(BaseModel):
    __test__ = False

    test_id: str
    status: TestStatus
    source: ExecutionResult
    target: ExecutionResult
    diffs: list[FieldDiff] = Field(default_factory=list)


# ---------------------------------------------------------------- repair, report, UI

class FailureReport(BaseModel):
    """What repair sends back into convert(obj, feedback=...)."""

    object_id: str
    attempt: int                       # 1-based; repair stops after 3
    failing_tests: list[TestOutcome]
    summary: str                       # one-paragraph plain-English diagnosis for the LLM prompt


class ValidationResult(BaseModel):
    object_id: str
    conversion_version: int
    tests: list[TestOutcome]
    passed: int
    failed: int
    status: ValidationStatus


# ---------------------------------------------------------------- repair context (D-6.7)

class PreviousAttempt(BaseModel):
    """The attempt being repaired, exactly as the converter returned it."""

    version: int = Field(ge=1)          # the ConversionResult.version being repaired
    tool: ConversionTool
    code: str                           # exactly what that attempt produced
    deployed: bool                      # did PostgreSQL accept it (compiled_ok)?
    deploy_error: str | None = None     # set only when deployed is False

    @model_validator(mode="after")
    def _deployed_has_no_error(self) -> PreviousAttempt:
        if self.deployed and self.deploy_error:
            raise ValueError("a deployed attempt has no deploy error")
        return self


class RepairContext(BaseModel):
    """What a repair attempt needs to know, built fresh by the repair loop for each repair.

    Kept apart from FailureReport, which stays a pure description of failing tests.
    Validation evidence and deploy errors never mix: the tests that failed are in
    ``last_validation`` (whose ``conversion_version`` says which version they ran on),
    the deploy error of the attempt being repaired is in ``previous.deploy_error``.
    """

    run_id: str = Field(min_length=1)   # one per repair-loop run
    object_id: str
    attempt: int = Field(ge=2)          # the loop attempt about to run
    previous: PreviousAttempt
    last_validation: ValidationResult | None = None

    @model_validator(mode="after")
    def _consistent(self) -> RepairContext:
        validation = self.last_validation
        if validation is None:
            return self
        if validation.object_id != self.object_id:
            raise ValueError(f"the last test run is for {validation.object_id}, not {self.object_id}")
        if validation.conversion_version > self.previous.version:
            raise ValueError(f"the last test run is for v{validation.conversion_version}, "
                             f"later than the previous attempt v{self.previous.version}")
        if validation.conversion_version == self.previous.version and not self.previous.deployed:
            raise ValueError(f"v{self.previous.version} did not deploy, so it cannot have been tested")
        return self


# ---------------------------------------------------------------- repair runs (moved here in 0.7)
#
# Moved unchanged from orchestration/repair_loop.py (which still re-exports them), so that
# reporting and storage read a repair run without importing the engine. Only the numeric
# bounds below are new: attempts and versions start at 1, counts are never negative.

class RepairOutcome(str, Enum):
    PASSED = "passed"                            # every test passed
    PASSED_NEEDS_REVIEW = "passed_needs_review"  # passed, but the converter flagged it for sign-off
    NEEDS_HUMAN = "needs_human"                  # stopped, out of attempts, or the harness could not judge


class FailureCategory(str, Enum):
    """Why a run ended NEEDS_HUMAN (or why one attempt failed)."""

    UNSUPPORTED = "unsupported"            # D5 REVIEW, not eligible, no rule, secret in source: a human's job
    NO_REPAIR_ROUTE = "no_repair_route"    # the converter cannot produce a different version
    DEPLOY_FAILED = "deploy_failed"        # PostgreSQL rejected the code
    DEPLOY_REFUSED = "deploy_refused"      # the deployer's safety checks refused the code
    TESTS_FAILED = "tests_failed"          # deployed, but behaves differently from Oracle
    GEMINI_FAILED = "gemini_failed"        # Gemini/API/Redis error, malformed answer, Gemini declined
    UNCHANGED_OUTPUT = "unchanged_output"  # Gemini returned the previous code again (D-6.4)
    INVALID_EVIDENCE = "invalid_evidence"  # the repair evidence did not match this run's attempt
    HARNESS_ERROR = "harness_error"        # the tests could not be judged
    CONVERTER_ERROR = "converter_error"    # unexpected error inside the converter


class AttemptRecord(BaseModel):
    """One convert + validate round, kept for the report."""

    attempt: int = Field(ge=1)
    version: int = Field(ge=1)
    tool: ConversionTool
    compiled_ok: bool                            # PostgreSQL deployed it (not: it behaves like Oracle)
    compile_error: str | None = None
    failure_code: str | None = None              # the converter's code when it did not deploy (7a)
    category: FailureCategory | None = None      # why this attempt failed, if it did (7a)
    model: str | None = None                     # the LLM asked for this attempt, if any (7a)
    needs_review: bool = False                   # the converter asked for human sign-off (7a)
    status: ValidationStatus | None = None       # None when it did not compile
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    failing_tests: list[str] = Field(default_factory=list)
    feedback_summary: str | None = None          # what was sent back for the next attempt


class RepairRun(BaseModel):
    """Everything the report needs about one object's trip through the loop."""

    object_id: str
    outcome: RepairOutcome
    reason: str
    attempts: list[AttemptRecord]
    final_conversion: ConversionResult | None = None
    final_validation: ValidationResult | None = None   # only a test run of final_conversion's version
    run_id: str | None = None                          # (7a) one per run; older reports have none
    category: FailureCategory | None = None            # (7a) why NEEDS_HUMAN; None when passed
    last_validation: ValidationResult | None = None    # (7a) the last test run, whatever version it tested
    last_deployed_version: int | None = None           # (7a) the version PostgreSQL holds after the run
