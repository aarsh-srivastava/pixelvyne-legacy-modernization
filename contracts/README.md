Exported JSON schemas of backend/app/contracts (see export_schemas.py).

# Canonical object identity (contract 0.2)

Agreed by Aarsh and Tarun on 10 Nov 2026. Implemented once, in
`backend/app/contracts/object_ids.py`; every stage imports it from `app.contracts`.
No component formats or splits object_ids by hand.

## Format

| Object | object_id |
| --- | --- |
| Table, view, sequence | `table:legacy.employees` |
| Standalone function, procedure, trigger | `procedure:legacy.calc_bonus` |
| Package (spec + body) | `package:legacy.payroll_pkg` |
| Package member | `function:legacy.payroll_pkg.net_pay` |
| Overloaded member | `procedure:hr.pkg.calculate(number,date)` |
| Reference not in the upload | `unresolved:legacy.bonus_rules`, `unresolved:pkg.fn` |

Two parts after the kind are `schema.object`; three are `schema.package.member`.
The object_id is the **source identity**. It is not the PostgreSQL name: the converter
chooses target names.

## Rules

1. **Lower-case, schema always present.** `normalize_identifier()` removes quotes and
   lower-cases. Unqualified source names get `default_schema(settings.ORACLE_USER)`,
   which falls back to `legacy`. The contracts layer never reads settings itself.
2. **Overloads.** The parser/analyzer decides that a name is overloaded and passes
   `signature=[raw types]`. `make_object_id()` never guesses. Types are lower-case,
   size and precision removed, in declaration order (`VARCHAR2(20)` -> `varchar2`).
   `signature=[]` gives `()` for a zero-argument overload.
3. **Collisions.** `"Emp"` and `EMP` both normalize to `emp`. Only the full set of
   discovered objects can reveal that, so the parser calls
   `check_unique_object_ids()`, which raises `ObjectIdCollisionError`. Distinct source
   objects are never merged silently.
4. **Unresolved references are kept, never guessed.** `make_unresolved_id()` adds the
   default schema to a 1-part reference only. `pkg.fn` stays `unresolved:pkg.fn`
   because it could be `schema.object` or `package.member`. The planner turns each into
   a `missing_dependency` blocker.
5. **Package constants and variables are not objects.** `payroll_pkg.c_allowance`
   makes the referencing object depend on `package:legacy.payroll_pkg`.
6. **No separate schema field.** `parse_object_id(obj.object_id).schema` gives it.

## Fields that hold identities

| Field | Holds |
| --- | --- |
| `IRObject.object_id` | the object's own canonical id |
| `IRObject.depends_on` | canonical ids of prerequisites, including `unresolved:` |
| `IRObject.reads_tables`, `writes_tables` | bare lower-case table names (watched by the harness) |
| `IRObject.trigger.table` | canonical id of the table or view a trigger fires on (never `unresolved:`) |
| `TestCase.object_id`, `ConversionResult.object_id`, ... | canonical ids |

## Triggers

`TriggerInfo` says what makes a trigger fire; `writes_tables` says what its body changes.
For `AFTER UPDATE OF base_salary ON employees FOR EACH ROW ... INSERT INTO salary_audit`:

- `trigger.table = "table:legacy.employees"` (also listed in `depends_on`)
- `trigger.timing = "after"`, `trigger.events = ["update"]`
- `trigger.update_columns = ["base_salary"]`, `trigger.for_each_row = True`
- `writes_tables = ["salary_audit"]`. The fired-on table is not a write.

`trigger.table` must be a resolved table or view id. If the fired-on table is not in the
upload, the trigger gets `trigger=None`, the reference stays in `depends_on` as
`unresolved:...`, and the planner raises a `missing_dependency` blocker.

## Tests

- `backend/tests/test_object_ids.py`: the helpers (round trips, overloads, package
  members, unresolved references, invalid ids, normalization, collisions, TriggerInfo).
- `backend/tests/test_object_id_conformance.py`: the parser's output for
  `samples/oracle` meets this contract. Marked `xfail(strict=True)` until PR B.
