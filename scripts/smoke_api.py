"""End-to-end smoke test of Steps 2-6, using only the Python standard library.

    python scripts/smoke_api.py            # API at http://localhost:8000
    API=http://host:8000 python scripts/smoke_api.py

It checks health, creates a project, uploads samples/oracle/*.sql as a ZIP, lists the
stored files, then runs a Celery ping job and polls it until it finishes.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

API = os.getenv("API", "http://localhost:8000").rstrip("/")
SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "oracle"


def call(method: str, path: str, body: bytes | None = None, content_type: str | None = None):
    req = urllib.request.Request(API + path, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def post_json(path: str, payload: dict):
    return call("POST", path, json.dumps(payload).encode(), "application/json")


def post_file(path: str, field: str, filename: str, data: bytes):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return call("POST", path, body, f"multipart/form-data; boundary={boundary}")


def expect(ok: bool, message: str) -> None:
    print(("  PASS  " if ok else "  FAIL  ") + message)
    if not ok:
        sys.exit(1)


def main() -> None:
    print(f"API: {API}")

    code, health = call("GET", "/api/v1/health")
    expect(code == 200 and health["status"] == "ok", f"health {health}")

    code, project = post_json("/api/v1/projects", {"name": "Employee & Payroll (sample)"})
    expect(code == 201, f"create project -> {project.get('id')}")
    pid = project["id"]

    buf = io.BytesIO()
    sql_files = sorted(SAMPLES.glob("*.sql"))
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sql_files:
            zf.write(f, f"legacy/{f.name}")
    code, saved = post_file(f"/api/v1/projects/{pid}/files", "files", "legacy.zip", buf.getvalue())
    expect(code == 201 and len(saved) == len(sql_files), f"upload ZIP -> {len(saved)} files stored")

    code, listing = call("GET", f"/api/v1/projects/{pid}/files")
    expect(code == 200 and [f["path"] for f in listing] == [f"legacy/{f.name}" for f in sql_files],
           "file listing matches upload")

    code, detail = call("GET", f"/api/v1/projects/{pid}/files/{listing[4]['id']}")
    expect(code == 200 and "calc_bonus" in detail["content"], f"read back {detail['path']}")

    code, job = call("POST", "/api/v1/jobs/ping?seconds=2")
    expect(code == 202, f"ping job queued -> {job['job_id']}")
    deadline = time.time() + 30
    seen = []
    while time.time() < deadline:
        _, status = call("GET", f"/api/v1/jobs/{job['job_id']}")
        if not seen or seen[-1] != (status["status"], status["stage"]):
            seen.append((status["status"], status["stage"]))
            print(f"        {status['status']:<10} {status['stage'] or ''}")
        if status["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.5)
    expect(status["status"] == "succeeded", f"ping finished on worker {status['result']}")

    print("\nSteps 2-6 work end to end.")


if __name__ == "__main__":
    main()
