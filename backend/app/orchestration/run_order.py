"""The order to convert and test objects in, so every test finds what it relies on (step 7b).

Dependency order (the analyzer's) is not enough for behaviour:

* a test's **setup** calls another object (pk-04 runs monthly_payroll before close_month),
  so that object must be converted and deployed first;
* a **trigger** fires on its table, so a routine whose tests write that table (apply_raise
  updates employees, which fires emp_salary_audit_trg) must be tested after the trigger is
  deployed; otherwise the missing audit row looks like apply_raise's fault.

Only objects with tests are returned; everything else keeps the analyzer's order.
"""

from __future__ import annotations

from typing import Callable, Iterable

from app.contracts import IRObject, ObjectType, TestCase, parse_object_id
from app.parsers.dependency_analyzer import analyze_dependencies


def in_test_order(objects: Iterable[IRObject], tests_for: Callable[[str], list[TestCase]]) -> list[IRObject]:
    """Objects that have tests, each after the objects its tests rely on."""
    objects = list(objects)
    by_id = {o.object_id: o for o in objects}
    triggers_on: dict[str, list[IRObject]] = {}
    for obj in objects:
        if obj.type == ObjectType.TRIGGER and obj.trigger is not None:
            triggers_on.setdefault(parse_object_id(obj.trigger.table).name.lower(), []).append(obj)

    placed: list[IRObject] = []
    visiting: set[str] = set()

    def place(obj: IRObject) -> None:
        if obj in placed or obj.object_id in visiting:
            return
        visiting.add(obj.object_id)
        for case in tests_for(obj.object_id):
            for step in case.setup_steps():
                if step.object_id in by_id:
                    place(by_id[step.object_id])
        for table in obj.writes_tables:
            for trigger in triggers_on.get(table.lower(), []):
                if trigger is not obj and tests_for(trigger.object_id):
                    place(trigger)
        visiting.discard(obj.object_id)
        placed.append(obj)

    for object_id in analyze_dependencies(objects).execution_order:
        if object_id in by_id and tests_for(object_id):
            place(by_id[object_id])
    return placed
