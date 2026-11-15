"""Contract 0.3: the optional ConversionResult.model field (Stage 2, step 6b-1).

The field is additive. Results written before 0.3 (no "model" key) must still load,
existing callers that never pass it must be unaffected, and the exported JSON Schema
must show it as optional.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts import CONTRACT_VERSION, ConversionResult, ConversionTool

BACKEND = Path(__file__).resolve().parents[1]

# A result exactly as contract 0.2 serialized it: no "model" key.
V02_JSON = json.dumps({
    "object_id": "procedure:legacy.calc_bonus",
    "version": 1,
    "tool": "sqlglot",
    "target_code": "CREATE OR REPLACE PROCEDURE calc_bonus() LANGUAGE plpgsql AS $$ BEGIN NULL; END; $$;",
    "compiled_ok": True,
    "compile_error": None,
    "warnings": [],
    "needs_review": False,
    "produced_at": "2026-11-10T12:00:00Z",
})


def test_version_is_at_least_0_3():
    assert tuple(map(int, CONTRACT_VERSION.split("."))) >= (0, 3)  # model was added in 0.3


def test_a_0_2_result_without_model_still_loads():
    result = ConversionResult.model_validate_json(V02_JSON)
    assert result.model is None
    assert result.tool is ConversionTool.SQLGLOT and result.compiled_ok and result.version == 1


def test_a_0_2_report_file_of_many_results_still_loads():
    """repair_runs.json-style lists written before 0.3 load unchanged."""
    rows = [json.loads(V02_JSON) | {"version": v} for v in (1, 2, 3)]
    results = [ConversionResult.model_validate(r) for r in rows]
    assert [r.version for r in results] == [1, 2, 3] and all(r.model is None for r in results)


def test_existing_callers_that_never_pass_model_are_unaffected():
    result = ConversionResult(object_id="view:legacy.org_chart", version=1, tool=ConversionTool.SQLGLOT,
                              target_code="", compiled_ok=False, compile_error="Not converted")
    assert result.model is None


def test_model_round_trips():
    result = ConversionResult(object_id="view:legacy.org_chart", version=1, tool=ConversionTool.GEMINI,
                              target_code="CREATE OR REPLACE VIEW org_chart AS SELECT 1;", compiled_ok=True,
                              needs_review=True, model="gemini-2.5-flash")
    again = ConversionResult.model_validate_json(result.model_dump_json())
    assert again == result and again.model == "gemini-2.5-flash"
    assert json.loads(result.model_dump_json())["model"] == "gemini-2.5-flash"


@pytest.mark.parametrize("bad", [5, 2.5, ["gemini"], {"name": "gemini"}])
def test_model_must_be_text_or_none(bad):
    with pytest.raises(ValidationError):
        ConversionResult.model_validate(json.loads(V02_JSON) | {"model": bad})


def test_the_json_schema_marks_model_optional():
    schema = ConversionResult.model_json_schema()
    assert "model" in schema["properties"]
    assert "model" not in schema.get("required", [])
    kinds = {option.get("type") for option in schema["properties"]["model"]["anyOf"]}
    assert kinds == {"string", "null"}


def test_the_schema_export_stamps_the_current_version(tmp_path):
    """export_schemas.py (the documented way to regenerate the JSON) runs and stamps the version."""
    subprocess.run([sys.executable, "-m", "app.contracts.export_schemas"], cwd=BACKEND, check=True,
                   env=os.environ | {"CONTRACTS_DIR": str(tmp_path)},
                   capture_output=True, text=True)
    schema = json.loads((tmp_path / "ConversionResult.schema.json").read_text())
    assert schema["$comment"] == f"contract version {CONTRACT_VERSION}"
    assert "model" in schema["properties"] and "model" not in schema.get("required", [])
    assert {p.name for p in tmp_path.glob("*.schema.json")} == {
        f"{name}.schema.json" for name in ("IRObject", "ConversionResult", "TestCase", "ExecutionResult",
                                           "TestOutcome", "FailureReport", "ValidationResult",
                                           "RepairContext")}
