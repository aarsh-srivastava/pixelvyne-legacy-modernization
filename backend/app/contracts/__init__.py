"""Stage contracts. Import from here rather than from another stage's internals."""

from app.contracts.interfaces import Analyzer, Converter
from app.contracts.object_ids import (
    OBJECT_ID_PATTERN,
    ObjectIdCollisionError,
    ObjectRef,
    check_unique_object_ids,
    default_schema,
    is_object_id,
    make_object_id,
    make_unresolved_id,
    normalize_identifier,
    normalize_signature,
    parse_object_id,
)
from app.contracts.models import (
    CONTRACT_VERSION,
    ConversionResult,
    ConversionTool,
    ExecutionError,
    ExecutionResult,
    FailureReport,
    FieldDiff,
    IRObject,
    ObjectType,
    Param,
    ParamMode,
    TableDiff,
    TestCase,
    TestOutcome,
    TestStatus,
    TriggerInfo,
    ValidationResult,
    ValidationStatus,
)

__all__ = [
    "Analyzer", "Converter", "CONTRACT_VERSION", "ConversionResult", "ConversionTool",
    "ExecutionError", "ExecutionResult", "FailureReport", "FieldDiff", "IRObject",
    "ObjectType", "Param", "ParamMode", "TableDiff", "TestCase", "TestOutcome",
    "TestStatus", "TriggerInfo", "ValidationResult", "ValidationStatus",
    # object identity (contract 0.2)
    "OBJECT_ID_PATTERN", "ObjectIdCollisionError", "ObjectRef", "check_unique_object_ids",
    "default_schema", "is_object_id", "make_object_id", "make_unresolved_id",
    "normalize_identifier", "normalize_signature", "parse_object_id",
]
