"""Schema metadata and the schema migration result (contract 0.7).

``TableDef`` is the one description of a table that both workstreams use: the schema
module (Workstream A, M3) produces it from the Oracle DDL, the engine (Workstream B) reads
column types from it instead of from its own DDL parsing (which is removed after M3), and
neither depends on the other's internals or on ``IRObject``.

``SchemaMigrator.migrate`` (interfaces.py) returns a ``SchemaMigrationResult``: one
``SchemaObjectOutcome`` per table and sequence it was asked about, each with a status and a
reason. Two modes:

* ``prepared``: the stand-in ``PreparedTargetSchema`` only checks a target that was
  prepared beforehand (seed_target.py and setup_target_roles.py). Nothing is created or
  copied, so statuses are ``present``, ``missing`` or ``mismatch`` and parity is not checked.
* ``migrated``: the real schema migrator (M3) creates the tables and sequences and copies
  the data: statuses are ``created``, ``mismatch`` or ``failed``, with row counts and, per
  table, a ``ParityCheck`` against Oracle. Parity is ``verified`` only when every table's
  row counts AND checksums match; matching row counts without checksums are the weaker
  ``rows_only`` (Aarsh's v0.7 review, 19 Nov).

Names: ``object_id`` is the source identity (``table:legacy.employees``, see object_ids.py);
``target_schema`` and ``target_name`` say where the object lives in PostgreSQL. Identifiers
are stored lower-case (normalize_identifier). Types are kept as text: ``oracle_type`` as
declared in the source DDL (``NUMBER(10,2)``), ``pg_type`` as PostgreSQL prints it
(``numeric(10,2)``), and ``default`` as a PostgreSQL expression.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contracts.object_ids import normalize_identifier, parse_object_id

_STRICT = ConfigDict(extra="forbid", frozen=True)


def _identifier(value: str) -> str:
    return normalize_identifier(value)


def _kind(object_id: str, kinds: set[str], what: str) -> str:
    if parse_object_id(object_id).kind not in kinds:
        raise ValueError(f"{what} must be a {' or '.join(sorted(kinds))} object_id, got {object_id!r}")
    return object_id


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class ColumnDef(BaseModel):
    model_config = _STRICT

    name: str
    oracle_type: str = Field(min_length=1)       # as declared in the Oracle DDL, e.g. "NUMBER(10,2)"
    pg_type: str = Field(min_length=1)           # as PostgreSQL prints it, e.g. "numeric(10,2)"
    nullable: bool = True
    default: str | None = None                   # PostgreSQL expression, e.g. "CURRENT_DATE"; None: no default

    _name = field_validator("name")(_identifier)


class ForeignKeyDef(BaseModel):
    model_config = _STRICT

    name: str | None = None                      # constraint name, when the source names it
    columns: list[str] = Field(min_length=1)
    ref_table: str                               # object_id of the referenced table
    ref_columns: list[str] = Field(min_length=1)
    on_delete: Literal["no_action", "restrict", "cascade", "set_null", "set_default"] = "no_action"

    @field_validator("columns", "ref_columns")
    @classmethod
    def _columns(cls, value: list[str]) -> list[str]:
        return [normalize_identifier(c) for c in value]

    @field_validator("ref_table")
    @classmethod
    def _ref_table(cls, value: str) -> str:
        return _kind(value, {"table"}, "ref_table")

    @model_validator(mode="after")
    def _same_length(self) -> ForeignKeyDef:
        if len(self.columns) != len(self.ref_columns):
            raise ValueError(f"foreign key has {len(self.columns)} columns but references {len(self.ref_columns)}")
        return self


class TableDef(BaseModel):
    """One table: its columns, keys and the target it lives in."""

    model_config = _STRICT

    object_id: str                               # "table:legacy.employees"
    target_schema: str = "public"
    target_name: str
    columns: list[ColumnDef] = Field(min_length=1)   # in declaration order
    primary_key: list[str] = Field(default_factory=list)
    unique: list[list[str]] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyDef] = Field(default_factory=list)

    _schema = field_validator("target_schema", "target_name")(_identifier)

    @field_validator("object_id")
    @classmethod
    def _object_id(cls, value: str) -> str:
        return _kind(value, {"table"}, "TableDef.object_id")

    @field_validator("primary_key")
    @classmethod
    def _pk(cls, value: list[str]) -> list[str]:
        return [normalize_identifier(c) for c in value]

    @field_validator("unique")
    @classmethod
    def _unique(cls, value: list[list[str]]) -> list[list[str]]:
        if any(not group for group in value):
            raise ValueError("a unique constraint needs at least one column")
        return [[normalize_identifier(c) for c in group] for group in value]

    @model_validator(mode="after")
    def _columns_known(self) -> TableDef:
        names = [c.name for c in self.columns]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate column(s): {', '.join(duplicates)}")
        used = list(self.primary_key) + [c for group in self.unique for c in group] + \
            [c for fk in self.foreign_keys for c in fk.columns]
        unknown = sorted(set(used) - set(names))
        if unknown:
            raise ValueError(f"key column(s) not in the table: {', '.join(unknown)}")
        return self

    def column(self, name: str) -> ColumnDef | None:
        name = normalize_identifier(name)
        return next((c for c in self.columns if c.name == name), None)


class SequenceDef(BaseModel):
    model_config = _STRICT

    object_id: str                               # "sequence:legacy.salary_audit_seq"
    target_schema: str = "public"
    target_name: str
    start_with: int = 1
    increment_by: int = 1
    min_value: int | None = None
    max_value: int | None = None
    cycle: bool = False

    _schema = field_validator("target_schema", "target_name")(_identifier)

    @field_validator("object_id")
    @classmethod
    def _object_id(cls, value: str) -> str:
        return _kind(value, {"sequence"}, "SequenceDef.object_id")

    @model_validator(mode="after")
    def _range(self) -> SequenceDef:
        if self.increment_by == 0:
            raise ValueError("increment_by must not be 0")
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError("min_value is greater than max_value")
        return self


class ParityCheck(BaseModel):
    """One table's data, Oracle against PostgreSQL after the copy (A15)."""

    model_config = _STRICT

    oracle_rows: int = Field(ge=0)
    target_rows: int = Field(ge=0)
    oracle_checksum: str | None = None           # same algorithm on both sides, e.g. md5 of ordered rows
    target_checksum: str | None = None

    @model_validator(mode="after")
    def _both_checksums_or_none(self) -> ParityCheck:
        if (self.oracle_checksum is None) != (self.target_checksum is None):
            raise ValueError("give both checksums or neither")
        return self

    @property
    def checksummed(self) -> bool:
        return self.oracle_checksum is not None

    @property
    def differs(self) -> bool:
        """Evidence of a difference: the row counts, or the checksums when both are given."""
        return self.oracle_rows != self.target_rows or self.oracle_checksum != self.target_checksum

    @property
    def matches(self) -> bool:
        """Full parity: row counts and checksums agree. Row counts alone never match."""
        return self.checksummed and not self.differs


class SchemaObjectStatus(str, Enum):
    CREATED = "created"      # migrated mode: created (and its data copied) by this run
    PRESENT = "present"      # prepared mode: already in the target as expected
    MISSING = "missing"      # prepared mode: not in the target
    MISMATCH = "mismatch"    # in the target, but not as the source defines it (columns, keys, rights, data)
    FAILED = "failed"        # migrated mode: could not be created or copied


#: Which statuses each mode may report.
MODE_STATUSES = {
    "prepared": {SchemaObjectStatus.PRESENT, SchemaObjectStatus.MISSING, SchemaObjectStatus.MISMATCH},
    "migrated": {SchemaObjectStatus.CREATED, SchemaObjectStatus.MISMATCH, SchemaObjectStatus.FAILED},
}
#: Statuses after which the engine may rely on the object.
READY = {SchemaObjectStatus.CREATED, SchemaObjectStatus.PRESENT}


class SchemaObjectOutcome(BaseModel):
    """The final status of one table or sequence, always with a reason."""

    model_config = _STRICT

    object_id: str
    status: SchemaObjectStatus
    reason: str
    definition: TableDef | SequenceDef | None = None   # what the target holds (or was created as)
    rows_copied: int | None = Field(default=None, ge=0)  # migrated mode, tables only
    parity: ParityCheck | None = None                     # migrated mode, tables only

    _reason = field_validator("reason")(_not_blank)

    @field_validator("object_id")
    @classmethod
    def _object_id(cls, value: str) -> str:
        return _kind(value, {"table", "sequence"}, "SchemaObjectOutcome.object_id")

    @model_validator(mode="after")
    def _consistent(self) -> SchemaObjectOutcome:
        is_table = parse_object_id(self.object_id).kind == "table"
        # same object_id, so a table gets a TableDef and a sequence a SequenceDef (each checks its kind)
        if self.definition is not None and self.definition.object_id != self.object_id:
            raise ValueError(f"definition is for {self.definition.object_id}, not {self.object_id}")
        if self.status in READY and self.definition is None:
            raise ValueError(f"a {self.status.value} object needs its definition")
        if self.status is SchemaObjectStatus.MISSING and self.definition is not None:
            raise ValueError("a missing object has no definition")
        if not is_table and (self.rows_copied is not None or self.parity is not None):
            raise ValueError("rows and parity apply to tables only")
        if self.parity is not None and self.parity.differs and self.status in READY:
            raise ValueError("a table whose data does not match Oracle is a mismatch, not ready")
        return self

    @property
    def ready(self) -> bool:
        return self.status in READY


class SchemaMigrationResult(BaseModel):
    model_config = _STRICT

    mode: Literal["prepared", "migrated"]
    migrator: str = Field(min_length=1)          # e.g. "PreparedTargetSchema"
    database: str = Field(min_length=1)          # the target database checked or migrated
    objects: list[SchemaObjectOutcome] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)   # target-wide: roles, helper, foreign objects, ...

    @model_validator(mode="after")
    def _consistent(self) -> SchemaMigrationResult:
        ids = [o.object_id for o in self.objects]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"more than one outcome for: {', '.join(duplicates)}")
        allowed = MODE_STATUSES[self.mode]
        wrong = [f"{o.object_id} ({o.status.value})" for o in self.objects if o.status not in allowed]
        if wrong:
            raise ValueError(f"{self.mode} mode cannot report: {', '.join(wrong)}")
        if self.mode == "prepared" and any(o.rows_copied is not None or o.parity is not None for o in self.objects):
            raise ValueError("prepared mode copies no data and checks no parity")
        if any(not p.strip() for p in self.problems):
            raise ValueError("a problem must not be blank")
        return self

    @property
    def ok(self) -> bool:
        """Every object ready and no target-wide problem: the engine may convert."""
        return not self.problems and all(o.ready for o in self.objects)

    @property
    def parity(self) -> Literal["verified", "rows_only", "mismatch", "not_checked"]:
        """A15 for the run: ``verified`` only when every table's row counts and checksums match;
        ``rows_only`` when every table was checked, none differs, but some lack checksums."""
        tables = [o for o in self.objects if parse_object_id(o.object_id).kind == "table"]
        if any(o.parity is not None and o.parity.differs for o in tables):
            return "mismatch"
        if self.mode == "migrated" and tables and all(o.parity is not None for o in tables):
            return "verified" if all(o.parity.matches for o in tables) else "rows_only"
        return "not_checked"

    def outcome(self, object_id: str) -> SchemaObjectOutcome | None:
        return next((o for o in self.objects if o.object_id == object_id), None)
