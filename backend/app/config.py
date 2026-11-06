"""All runtime settings, read from environment variables (docker-compose passes .env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Platform database (SQLAlchemy URL, managed by Alembic)
    APP_DATABASE_URL: str = "postgresql+psycopg://pixelvyne:pixelvyne@localhost:5432/pixelvyne"

    # Target sandbox (plain libpq URL for psycopg)
    TARGET_DATABASE_URL: str = "postgresql://sandbox:sandbox@localhost:5433/target"

    # Legacy Oracle source
    ORACLE_DSN: str = "localhost:1521/FREEPDB1"
    ORACLE_USER: str = "legacy"
    ORACLE_PASSWORD: str = "legacy"

    # Celery + cache
    REDIS_URL: str = "redis://localhost:6379/0"

    # LLM
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # API
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]
    MAX_UPLOAD_MB: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
