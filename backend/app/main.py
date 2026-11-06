"""FastAPI entry point."""
from fastapi.openapi.utils import get_openapi
import redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import __version__
from app.api.projects import router as projects_router
from app.config import settings
from app.database.session import engine
from app.orchestration.router import router as jobs_router

app = FastAPI(
    title="PixelVyne Modernize",
    version=__version__,
    description="Agentic Oracle -> PostgreSQL migration with verified functional equivalence.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects_router)
app.include_router(jobs_router)


@app.get("/api/v1/health", tags=["health"])
def health() -> dict:
    """Liveness plus a quick check of the two services the API needs."""
    checks: dict[str, str] = {}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["app_db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["app_db"] = f"error: {exc.__class__.__name__}"
    try:
        redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2).ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc.__class__.__name__}"
    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "version": __version__, "checks": checks}
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # Use OpenAPI 3.0 file-upload representation for Swagger UI.
    schema["openapi"] = "3.0.3"

    upload_schema = schema.get("components", {}).get("schemas", {}).get(
        "Body_upload_files_api_v1_projects__project_id__files_post"
    )

    if upload_schema:
        files_schema = upload_schema["properties"]["files"]
        files_schema["items"]["format"] = "binary"
        files_schema["items"].pop("contentMediaType", None)

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi