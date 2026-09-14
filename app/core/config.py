"""Application settings, loaded from the environment via pydantic-settings."""

from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import AnyHttpUrl, BeforeValidator, PostgresDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: Any) -> Any:
    """Allow CORS origins to be given as a comma-separated string or a JSON list."""
    if isinstance(value, str) and not value.startswith("["):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CorsOrigins = Annotated[list[AnyHttpUrl], BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # --- App ---------------------------------------------------------------
    PROJECT_NAME: str = "LeadPilot API"
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Security ----------------------------------------------------------
    # Must be at least 32 bytes for HS256 (RFC 7518). Generate with:
    #   python -c "import secrets; print(secrets.token_urlsafe(32))"
    SECRET_KEY: str = "insecure-development-key-replace-me-in-your-env-file"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 30
    JWT_ALGORITHM: str = "HS256"

    # The Vite dev server origin. Override in .env for other environments.
    BACKEND_CORS_ORIGINS: CorsOrigins = []

    # --- Database ----------------------------------------------------------
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "leadpilot"

    # Set this to override the assembled URL entirely (e.g. a hosted DB, or
    # "sqlite+aiosqlite:///./leadpilot.db" for a zero-setup local run).
    DATABASE_URL: str | None = None

    SQL_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_database_uri(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                host=self.POSTGRES_SERVER,
                port=self.POSTGRES_PORT,
                path=self.POSTGRES_DB,
            )
        )


@lru_cache
def get_settings() -> Settings:
    """Cached so the environment is parsed once per process."""
    return Settings()


settings = get_settings()
