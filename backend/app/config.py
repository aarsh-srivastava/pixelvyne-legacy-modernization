"""All runtime settings, read from environment variables (docker-compose passes .env)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Platform database (SQLAlchemy URL, managed by Alembic)
    APP_DATABASE_URL: str = "postgresql+psycopg://pixelvyne:pixelvyne@localhost:5432/pixelvyne"

    # Target sandbox (plain libpq URL for psycopg)
    TARGET_DATABASE_URL: str = "postgresql://sandbox:sandbox@localhost:5433/target"
    # The target database's name, set on its own: run_repair.py's pre-flight and the deployer check
    # each connection against it, so a URL pointing at another database is caught, not trusted.
    TARGET_DATABASE_NAME: str = Field(default="target", min_length=1)

    # Legacy Oracle source
    ORACLE_DSN: str = "localhost:1521/FREEPDB1"
    ORACLE_USER: str = "legacy"
    ORACLE_PASSWORD: str = "legacy"

    # Celery + cache
    REDIS_URL: str = "redis://localhost:6379/0"

    # LLM
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    # Per request, in seconds. The SDK also sends it to Google as X-Server-Timeout, so it is both
    # our client limit and the server's deadline (llm_service.py). Outside 10-600: refused at start-up.
    GEMINI_TIMEOUT_SECONDS: int = Field(default=120, ge=10, le=600)

    # API
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]
    MAX_UPLOAD_MB: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
