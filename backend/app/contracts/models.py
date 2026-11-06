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

from pydantic import BaseModel, Field

CONTRACT_VERSION = "0.1"


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


class IRObject(BaseModel):
    """One source object, as understood by the analysis step."""

    object_id: str                     # stable id, e.g. "procedure:calc_bonus"
    name: str                          # lower-case, unqualified
    type: ObjectType
    package: str | None = None         # set for members of a package
    params: list[Param] = Field(default_factory=list)
    return_type: str | None = None     # functions only, Oracle type
    reads_tables: list[str] = Field(default_factory=list)
    writes_tables: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)   # other object_ids
    business_rules: list[str] = Field(default_factory=list)
    source_code: str
    source_file: str | None = None
    oracle_features: list[str] = Field(default_factory=list)  # e.g. ["DECODE", "ROWNUM"]


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
