"""The end-to-end pipeline contract (0.7): what goes into a migration run and what comes out.

    sources (SourceFile) + MigrationOptions + TargetEnvironment
        -> MigrationEngine.run_migration(...)          (Workstream B, orchestration/)
              uses SchemaMigrator.migrate(...)          (A: schema/; stand-in: PreparedTargetSchema)
        -> MigrationRun                                 (read by A: reports, storage, API)

**Final status of every object (no silent skips, A17).** ``MigrationRun.objects`` holds
exactly one ``ObjectOutcome`` per object the engine discovered in the sources: the
validator refuses a run where any discovered object is missing or reported twice. Every
outcome has an ``ObjectStatus`` and a non-blank reason:

* ``passed``: deployed, and every test of the final version passed (tables and sequences:
  ready in the target, see schema_defs.py).
* ``passed_needs_review``: all tests passed, but a human must still sign it off (the
  converter flagged it, Gemini wrote it, or no test ran).
* ``needs_human``: tried and stopped: tests still fail, no repair route, could not be
  judged, or the target object is missing or wrong. ``category`` says which, when known.
* ``not_converted``: deliberately not attempted: not selected, blocked by an object it
  depends on (``blocked_by``), the run did not start (``MigrationRun.status`` not_run), or
  nothing to deploy on its own (a package container, D1).
* ``error``: the engine itself failed on this object, outside the repair loop. Always
  counted as unexplained (A16).

**Routes (A2).** How the engine classified the object: ``rules`` (deterministic
translation), ``gemini`` (needs the Gemini fallback), ``review`` (a human's job, D5),
``schema`` (tables and sequences, handled by the SchemaMigrator), ``none`` (no route:
not attempted). A Gemini-route object stays on the ``gemini`` route when Gemini is off;
it then ends needs_human with the reason.

**Errors.** ``run_migration`` never raises for one object: a failure is that object's
outcome. It returns a run with status ``not_run`` (and ``problems``) when the target is not
ready, and raises ``MigrationInputError`` only for unusable input (no sources, duplicate
names, a source that does not match its hash). Everything else it raises is a bug.

**Secrets.** ``TargetEnvironment`` holds passwords and URLs as ``SecretStr``; it is passed
to the engine and never stored: ``MigrationRun`` has no field that can hold it.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from app.contracts.models import (
    CONTRACT_VERSION,
    ConversionTool,
    FailureCategory,
    ObjectType,
    RepairOutcome,
    RepairRun,
)
from app.contracts.object_ids import (
    UNRESOLVED,
    is_object_id,
    normalize_identifier,
    parse_object_id,
)
from app.contracts.schema_defs import SchemaMigrationResult, SchemaObjectOutcome

_STRICT = ConfigDict(extra="forbid", frozen=True)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VERSION = re.compile(r"^\d+\.\d+$")


class MigrationInputError(ValueError):
    """The input to run_migration cannot be used (nothing was converted or deployed)."""


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _source_object_id(value: str) -> str:
    if not is_object_id(value) or parse_object_id(value).kind == UNRESOLVED:
        raise ValueError(f"not the object_id of a source object: {value!r}")
    return value


# ---------------------------------------------------------------- inputs

class SourceFile(BaseModel):
    """One Oracle source file, from any SourceProvider (upload, live Oracle, a folder)."""

    model_config = _STRICT

    name: str = Field(min_length=1, max_length=255)   # relative, '/'-separated, e.g. "03_payroll_pkg.sql"
    text: str
    sha256: str                                      # of text encoded as UTF-8

    @classmethod
    def of(cls, name: str, text: str) -> SourceFile:
        return cls(name=name, text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())

    @field_validator("name")
    @classmethod
    def _relative_name(cls, value: str) -> str:
        path = PurePosixPath(value)
        if "\\" in value or "\0" in value or path.is_absolute() or ".." in path.parts or value != str(path):
            raise ValueError(f"source names are plain relative paths, got {value!r}")
        return value

    @model_validator(mode="after")
    def _hash_matches(self) -> SourceFile:
        if not _SHA256.match(self.sha256):
            raise ValueError("sha256 must be 64 lower-case hex digits")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError(f"sha256 does not match the text of {self.name}")
        return self

    def ref(self) -> SourceRef:
        return SourceRef(name=self.name, sha256=self.sha256, chars=len(self.text))


class SourceRef(BaseModel):
    """What a run records about a source: enough to know which text it ran on, not the text."""

    model_config = _STRICT

    name: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=_SHA256.pattern)
    chars: int = Field(ge=0)


class MigrationOptions(BaseModel):
    model_config = _STRICT

    objects: list[str] | None = Field(default=None, min_length=1)  # names or object_ids; None: every object
    gemini: bool = False                          # Gemini calls only when True (and a key is configured)
    gemini_request_budget: int = Field(default=0, ge=0, le=1000)  # most requests sent to Gemini in the run
    with_edge_cases: bool = False                 # add generated cases to the tests (step 7d)
    max_repair_attempts: int = Field(default=3, ge=1, le=5)

    @field_validator("objects")
    @classmethod
    def _names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [v if is_object_id(v) else normalize_identifier(v) for v in value]

    @model_validator(mode="after")
    def _budget(self) -> MigrationOptions:
        if self.gemini and self.gemini_request_budget < 1:
            raise ValueError("with gemini on, state a request budget of at least 1")
        if not self.gemini and self.gemini_request_budget:
            raise ValueError("a request budget needs gemini on")
        return self


class TargetEnvironment(BaseModel):
    """Where to read from and deploy to. Built by the caller from settings; never stored.

    Passwords and URLs are ``SecretStr``: they print and serialise as ``**********`` and
    are read only with ``.get_secret_value()``. Validation errors never echo the input.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    oracle_dsn: str = Field(min_length=1)         # host:port/service, no credentials
    oracle_user: str = Field(min_length=1)
    oracle_password: SecretStr
    deployer_url: SecretStr                       # pv_deployer: deploys converted objects
    runner_url: SecretStr                         # pv_runner: runs the tests
    admin_url: SecretStr | None = None            # creates tables and roles (M3); not needed when prepared
    database_name: str                            # TARGET_DATABASE_NAME: every connection must be on it
    redis_url: SecretStr | None = None            # the Gemini cache; required when options.gemini

    @field_validator("deployer_url", "runner_url", "admin_url")
    @classmethod
    def _postgres_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith(("postgresql://", "postgres://")):
            raise ValueError("must be a postgresql:// URL")
        return value

    @field_validator("database_name")
    @classmethod
    def _database(cls, value: str) -> str:
        return normalize_identifier(value)


# ---------------------------------------------------------------- per object

class ObjectStatus(str, Enum):
    PASSED = "passed"
    PASSED_NEEDS_REVIEW = "passed_needs_review"
    NEEDS_HUMAN = "needs_human"
    NOT_CONVERTED = "not_converted"
    ERROR = "error"


class Route(str, Enum):
    RULES = "rules"
    GEMINI = "gemini"
    REVIEW = "review"
    SCHEMA = "schema"
    NONE = "none"


SCHEMA_TYPES = frozenset({ObjectType.TABLE, ObjectType.SEQUENCE})
SUCCESS = frozenset({ObjectStatus.PASSED, ObjectStatus.PASSED_NEEDS_REVIEW})


class GeminiCall(BaseModel):
    """One Gemini request or cache hit (llm_service.CallRecord). Sizes only, never text."""

    model_config = _STRICT

    model: str = Field(min_length=1)
    attempt: int = Field(ge=0, le=10)             # 1.. for a request; 0 for a cache hit
    elapsed_ms: int = Field(ge=0)
    outcome: str = Field(pattern=r"^(ok|cache_hit|client_timeout|http_\d{3}|error:[A-Za-z_][A-Za-z0-9_]*)$")
    timeout_s: int = Field(ge=1)
    prompt_chars: int = Field(ge=0)
    response_chars: int | None = Field(default=None, ge=0)   # None: no answer received
    cache: Literal["hit", "stored", "write_failed", "not_stored", "off"]

    @model_validator(mode="after")
    def _consistent(self) -> GeminiCall:
        hit = (self.attempt == 0, self.outcome == "cache_hit", self.cache == "hit")
        if len(set(hit)) != 1:
            raise ValueError("a cache hit has attempt 0, outcome cache_hit and cache hit, and nothing else does")
        answered = self.outcome in ("ok", "cache_hit")
        if answered != (self.response_chars is not None):
            raise ValueError("response_chars is set exactly when an answer was received")
        return self

    @property
    def is_request(self) -> bool:
        return self.attempt > 0


def status_for_repair(run: RepairRun) -> ObjectStatus:
    """The ObjectStatus a repair run ends in. PASSED counts as passed only if tests ran and all passed."""
    if run.outcome is RepairOutcome.NEEDS_HUMAN:
        return ObjectStatus.NEEDS_HUMAN
    tested = run.final_validation
    if (run.outcome is RepairOutcome.PASSED and tested is not None and tested.tests
            and tested.passed == len(tested.tests)):
        return ObjectStatus.PASSED
    return ObjectStatus.PASSED_NEEDS_REVIEW


def status_for_schema(outcome: SchemaObjectOutcome) -> ObjectStatus:
    return ObjectStatus.PASSED if outcome.ready else ObjectStatus.NEEDS_HUMAN


class ObjectOutcome(BaseModel):
    """The final status of one source object, with its route, reason and evidence."""

    model_config = _STRICT

    object_id: str
    type: ObjectType
    source_file: str | None = None
    route: Route
    status: ObjectStatus
    reason: str
    category: FailureCategory | None = None       # why it needs a human, when known
    blocked_by: list[str] = Field(default_factory=list)   # object_ids that kept it from being converted
    repair: RepairRun | None = None               # every attempt and test run (routines and views)
    gemini_calls: list[GeminiCall] = Field(default_factory=list)

    _object_id = field_validator("object_id")(_source_object_id)
    _reason = field_validator("reason")(_not_blank)

    @field_validator("blocked_by")
    @classmethod
    def _blocked_by(cls, value: list[str]) -> list[str]:
        for object_id in value:
            if not is_object_id(object_id):
                raise ValueError(f"blocked_by takes object_ids, got {object_id!r}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ObjectOutcome:
        if parse_object_id(self.object_id).kind != self.type.value:
            raise ValueError(f"{self.object_id} is not a {self.type.value}")
        if (self.route is Route.SCHEMA) != (self.type in SCHEMA_TYPES):
            raise ValueError("tables and sequences, and only they, take the schema route")
        if self.status in SUCCESS and self.category is not None:
            raise ValueError("a passed object has no failure category")
        if self.blocked_by and self.status is not ObjectStatus.NOT_CONVERTED:
            raise ValueError("an object blocked by another one is not_converted")
        if self.route is Route.REVIEW and self.status in SUCCESS:
            raise ValueError("a review-route object cannot pass without a human")
        used_gemini = bool(self.gemini_calls) or (
            self.repair is not None and any(a.tool is ConversionTool.GEMINI for a in self.repair.attempts))
        if used_gemini and self.route is not Route.GEMINI:
            raise ValueError("only gemini-route objects call Gemini")
        if self.repair is not None:
            if self.route not in (Route.RULES, Route.GEMINI, Route.REVIEW):
                raise ValueError(f"a {self.route.value}-route object has no repair run")
            if self.repair.object_id != self.object_id:
                raise ValueError(f"the repair run is for {self.repair.object_id}, not {self.object_id}")
            expected = status_for_repair(self.repair)
            if self.status is not expected:
                raise ValueError(f"the repair run ends {expected.value}, not {self.status.value}")
            if self.category is not self.repair.category:
                raise ValueError("category must be the repair run's category")
        elif self.route is not Route.SCHEMA and self.status not in (ObjectStatus.NOT_CONVERTED, ObjectStatus.ERROR):
            raise ValueError(f"{self.status.value} needs the repair run as evidence")
        return self

    @property
    def deployed(self) -> bool:
        return self.repair is not None and self.repair.last_deployed_version is not None

    @property
    def validated(self) -> bool:
        """The final version was tested, with at least one test (A14 evidence)."""
        tested = self.repair.final_validation if self.repair else None
        return tested is not None and len(tested.tests) > 0


# ---------------------------------------------------------------- the run

class StatusCounts(BaseModel):
    model_config = _STRICT

    passed: int = Field(default=0, ge=0)
    passed_needs_review: int = Field(default=0, ge=0)
    needs_human: int = Field(default=0, ge=0)
    not_converted: int = Field(default=0, ge=0)
    error: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.passed + self.passed_needs_review + self.needs_human + self.not_converted + self.error

    @property
    def migrated(self) -> int:
        """A12: deployed (or ready) and all tests passed, sign-off pending or not."""
        return self.passed + self.passed_needs_review

    @classmethod
    def of(cls, outcomes: list[ObjectOutcome]) -> StatusCounts:
        return cls(**{s.value: sum(o.status is s for o in outcomes) for s in ObjectStatus})


UNEXPLAINED = frozenset({FailureCategory.HARNESS_ERROR, FailureCategory.CONVERTER_ERROR})
DEPLOY_FAILURES = frozenset({FailureCategory.DEPLOY_FAILED, FailureCategory.DEPLOY_REFUSED})


class RunMetrics(BaseModel):
    """Acceptance metrics A12-A17, computed from the outcomes (``RunMetrics.of``), never set by hand.

    Percentages are left to the reader: counts are exact, and a reader divides by
    ``objects.total`` (A12, A13) or ``deployed`` (A14).
    """

    model_config = _STRICT

    objects: StatusCounts                         # A12, A13 over every source object
    by_type: dict[ObjectType, StatusCounts]       # A12 per object type
    needs_human: list[str]                        # A13: each listed (reason and evidence in its outcome)
    deployed: int = Field(ge=0)                   # A14: routines and views PostgreSQL holds a version of
    validated: int = Field(ge=0)                  # A14: of those, final version tested with >= 1 test
    schema_parity: Literal["verified", "rows_only", "mismatch", "not_checked"]   # A15
    failed_deployments: int = Field(ge=0)         # A16: attempts PostgreSQL rejected or the deployer refused
    unexplained: list[str]                        # A16: must be empty: status error, harness or converter error
    gemini_requests: int = Field(ge=0)            # A4: requests sent (cache hits excluded)
    gemini_cache_hits: int = Field(ge=0)
    source_objects: int = Field(ge=0)             # A17: discovered in the sources ...
    reported_objects: int = Field(ge=0)           # ... and reported; equal in every valid run

    @classmethod
    def of(cls, objects: list[ObjectOutcome], schema: SchemaMigrationResult, source_objects: int) -> RunMetrics:
        calls = [c for o in objects for c in o.gemini_calls]
        return cls(
            objects=StatusCounts.of(objects),
            by_type={t: StatusCounts.of([o for o in objects if o.type is t])
                     for t in ObjectType if any(o.type is t for o in objects)},
            needs_human=[o.object_id for o in objects if o.status is ObjectStatus.NEEDS_HUMAN],
            deployed=sum(o.deployed for o in objects),
            validated=sum(o.deployed and o.validated for o in objects),
            schema_parity=schema.parity,
            failed_deployments=sum(1 for o in objects if o.repair for a in o.repair.attempts
                                   if a.category in DEPLOY_FAILURES),
            unexplained=[o.object_id for o in objects
                         if o.status is ObjectStatus.ERROR or o.category in UNEXPLAINED],
            gemini_requests=sum(c.is_request for c in calls),
            gemini_cache_hits=sum(not c.is_request for c in calls),
            source_objects=source_objects,
            reported_objects=len(objects),
        )


class RunStatus(str, Enum):
    COMPLETED = "completed"   # every object was taken as far as it could go
    NOT_RUN = "not_run"       # the target was not ready (see problems): nothing was converted


class MigrationRun(BaseModel):
    """Everything one migration run did, for reports, storage and the API.

    The validator enforces the contract's promises: one outcome per discovered object (A17),
    schema outcomes that agree with the SchemaMigrationResult, no Gemini call with Gemini off
    and no more requests than the budget, no more attempts than allowed, and metrics that are
    exactly ``RunMetrics.of`` the outcomes.
    """

    model_config = _STRICT

    contract_version: str = Field(default=CONTRACT_VERSION, pattern=_VERSION.pattern)   # exactly CONTRACT_VERSION
    run_id: str = Field(pattern=_RUN_ID.pattern)
    status: RunStatus
    started_at: AwareDatetime
    finished_at: AwareDatetime
    engine: str = Field(min_length=1)             # what produced the outcomes, e.g. "OracleToPostgresConverter"
    stand_in: bool                                # True when a stand-in or fake produced any outcome
    oracle_answers: Literal["live", "recorded"]   # tests judged against live Oracle, or its recorded answers
    options: MigrationOptions
    sources: list[SourceRef] = Field(min_length=1)
    discovered: list[str]                         # object_ids parsed from the sources
    schema_result: SchemaMigrationResult
    objects: list[ObjectOutcome]
    problems: list[str] = Field(default_factory=list)   # why the run did not start, or run-wide notes
    metrics: RunMetrics

    _discovered = field_validator("discovered")(lambda value: [_source_object_id(v) for v in value])

    @field_validator("contract_version")
    @classmethod
    def _this_version(cls, value: str) -> str:
        # A run is read only by the contract that wrote it: another version needs an explicit
        # migration of the stored run, never a silent read (Aarsh's v0.7 review, 19 Nov).
        if value != CONTRACT_VERSION:
            raise ValueError(f"this is contract {CONTRACT_VERSION}; a run written for contract {value} "
                             "must be migrated to it first")
        return value

    @model_validator(mode="after")
    def _promises(self) -> MigrationRun:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        names = [s.name for s in self.sources]
        if len(set(names)) != len(names):
            raise ValueError("source names must be unique")
        self._no_silent_skips()
        for outcome in self.objects:
            if outcome.source_file is not None and outcome.source_file not in names:
                raise ValueError(f"{outcome.object_id} names a source that is not in the run: {outcome.source_file}")
            if outcome.repair is not None and len(outcome.repair.attempts) > self.options.max_repair_attempts:
                raise ValueError(f"{outcome.object_id} took more than {self.options.max_repair_attempts} attempts")
        self._schema_agrees()
        self._gemini_within_limits()
        if self.status is RunStatus.NOT_RUN:
            if not self.problems:
                raise ValueError("a run that did not start says why (problems)")
            started = [o.object_id for o in self.objects
                       if o.route is not Route.SCHEMA and o.status is not ObjectStatus.NOT_CONVERTED]
            if started:
                raise ValueError(f"the run did not start, yet these were converted: {', '.join(started)}")
        if any(not p.strip() for p in self.problems):
            raise ValueError("a problem must not be blank")
        expected = RunMetrics.of(self.objects, self.schema_result, len(self.discovered))
        if self.metrics != expected:
            raise ValueError("metrics must be RunMetrics.of(objects, schema_result, len(discovered))")
        return self

    def _no_silent_skips(self) -> None:
        discovered, reported = self.discovered, [o.object_id for o in self.objects]
        twice = sorted({i for ids in (discovered, reported) for i in ids if ids.count(i) > 1})
        missing, extra = sorted(set(discovered) - set(reported)), sorted(set(reported) - set(discovered))
        if twice or missing or extra:
            parts = [f"{label}: {', '.join(ids)}" for label, ids in
                     (("listed twice", twice), ("no final status", missing), ("not discovered", extra)) if ids]
            raise ValueError("every discovered object needs exactly one outcome (A17); " + "; ".join(parts))

    def _schema_agrees(self) -> None:
        schema_ids = {o.object_id for o in self.objects if o.route is Route.SCHEMA}
        result_ids = {o.object_id for o in self.schema_result.objects}
        if schema_ids != result_ids:
            raise ValueError("the schema result must cover exactly the tables and sequences: "
                             f"{', '.join(sorted(schema_ids ^ result_ids))}")
        for outcome in self.objects:
            if outcome.route is Route.SCHEMA:
                expected = status_for_schema(self.schema_result.outcome(outcome.object_id))
                if outcome.status is not expected:
                    raise ValueError(f"{outcome.object_id}: the schema result makes it {expected.value}, "
                                     f"not {outcome.status.value}")

    def _gemini_within_limits(self) -> None:
        calls = [c for o in self.objects for c in o.gemini_calls]
        if calls and not self.options.gemini:
            raise ValueError("Gemini was off, yet Gemini calls are recorded")
        requests = sum(c.is_request for c in calls)
        if requests > self.options.gemini_request_budget and self.options.gemini:
            raise ValueError(f"{requests} Gemini requests exceed the budget of {self.options.gemini_request_budget}")

    def outcome(self, object_id: str) -> ObjectOutcome | None:
        return next((o for o in self.objects if o.object_id == object_id), None)
