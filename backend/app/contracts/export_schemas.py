"""Write JSON Schemas for every contract model into the repo's contracts/ folder.

Run on your laptop from backend/ (needs pydantic):  python -m app.contracts.export_schemas
Inside Docker only backend/ is mounted, so set CONTRACTS_DIR=/code/schemas there instead.
The Pydantic models remain the source of truth; the JSON is for the frontend and the deck.
"""

import json
import os
from pathlib import Path

from app.contracts import models

OUT = Path(os.getenv("CONTRACTS_DIR", Path(__file__).resolve().parents[3] / "contracts"))

EXPORT = [
    models.IRObject, models.ConversionResult, models.TestCase, models.ExecutionResult,
    models.TestOutcome, models.FailureReport, models.ValidationResult,
]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for model in EXPORT:
        path = OUT / f"{model.__name__}.schema.json"
        schema = model.model_json_schema()
        schema["$comment"] = f"contract version {models.CONTRACT_VERSION}"
        path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(OUT.parent)}")


if __name__ == "__main__":
    main()
