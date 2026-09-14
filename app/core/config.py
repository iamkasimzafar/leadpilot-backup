"""Application settings, loaded from the environment via pydantic-settings."""

import json
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import quote_plus

from pydantic import AnyHttpUrl, BeforeValidator, computed_field
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: Any) -> Any:
    """Allow CORS origins to be given as a comma-separated string or a JSON list."""
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("["):
            return json.loads(raw)
        return [item.strip() for item in raw.split(",") if item.strip()]
    return value


# NoDecode stops pydantic-settings from running json.loads on the raw env value
# before _split_csv sees it -- without it, a plain comma-separated string fails.
CorsOrigins = Annotated[list[AnyHttpUrl], NoDecode, BeforeValidator(_split_csv)]


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

    # --- Frontend ----------------------------------------------------------
    # Used to build the links we email out (password reset, email verification).
    FRONTEND_URL: str = "http://localhost:5173"

    # --- Email (SMTP) ------------------------------------------------------
    # With EMAIL_ENABLED=false nothing is sent: the message is logged instead,
    # so the whole flow is testable locally without credentials.
    EMAIL_ENABLED: bool = False
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_STARTTLS: bool = True
    SMTP_TIMEOUT: int = 15
    EMAIL_FROM_ADDRESS: str = ""
    EMAIL_FROM_NAME: str = "LeadPilot"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def email_from(self) -> str:
        """RFC 5322 From header. Gmail rewrites a From that isn't the SMTP user,
        so fall back to SMTP_USER when no explicit address is configured."""
        address = self.EMAIL_FROM_ADDRESS or self.SMTP_USER
        return f"{self.EMAIL_FROM_NAME} <{address}>" if address else self.EMAIL_FROM_NAME

    # --- Database (MySQL) --------------------------------------------------
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = ""
    MYSQL_DB: str = "LeadPilot"

    # Set this to override the assembled URL entirely (e.g. a hosted DB, or
    # "sqlite+aiosqlite:///./leadpilot.db" for a zero-setup local run).
    DATABASE_URL: str | None = None

    SQL_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 3600  # MySQL drops idle connections after wait_timeout.

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_database_uri(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        # quote_plus so passwords containing @ : / # survive URL parsing.
        password = quote_plus(self.MYSQL_PASSWORD)
        return (
            f"mysql+asyncmy://{self.MYSQL_USER}:{password}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    """Cached so the environment is parsed once per process."""
    return Settings()


settings = get_settings()
