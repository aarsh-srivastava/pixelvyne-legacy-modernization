"""Internal contracts between the pipeline stages.

Keeping stages talking only through these models lets each one be built, faked and
tested on its own. Bump CONTRACT_VERSION whenever a field changes.

Flow of data:
    analysis    -> IRObject
    conversion  -> ConversionResult
    validation  -> TestCase, ExecutionResult, TestOutcome, ValidationResult
    repair      -> FailureReport, fed back into convert(obj, feedback=...)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.contracts.object_ids import normalize_identifier, parse_object_id

CONTRACT_VERSION = "0.2"   # 0.2: canonical object_ids (object_ids.py), TriggerInfo


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
    object_id: str
    version: int                       # 1, 2, 3... one per attempt, never overwritten
    tool: ConversionTool
    target_code: str
    compiled_ok: bool
    compile_error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    needs_review: bool = False
    produced_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------- validation

class TestCase(BaseModel):
    __test__ = False  # not a pytest class

    test_id: str                       # e.g. "cb-02"
    object_id: str
    description: str
    inputs: dict[str, Any] = Field(default_factory=dict)          # IN params by name
    fixture_rows: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)  # table -> rows to insert first
    setup_calls: list[str] = Field(default_factory=list)          # object_ids to run first (e.g. payroll before close_month)
    frozen_now: datetime | None = None                            # replaces SYSDATE / now()
    origin: Literal["golden", "rule", "llm"] = "rule"
    rationale: str | None = None


class ExecutionError(BaseModel):
    code: str | None = None            # e.g. "ORA-01403", "P0002"
    message: str


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
