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

    # --- Google sign-in ----------------------------------------------------
    # The OAuth 2.0 *Web* client ID from Google Cloud Console. It must be the
    # same value the frontend uses (VITE_GOOGLE_CLIENT_ID): tokens issued to any
    # other client are rejected. Empty switches "Continue with Google" off.
    GOOGLE_CLIENT_ID: str = ""

    # The desktop app signs in through its own *Desktop app* OAuth client
    # (Google only lets that type redirect to a loopback port), so its tokens
    # carry a different audience. Empty means only the web client is accepted.
    GOOGLE_DESKTOP_CLIENT_ID: str = ""

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

    # --- DeepSeek (AI keyword expansion) -----------------------------------
    # The API is OpenAI-compatible; the base URL only changes for a proxy.
    # With no key set the expand endpoint answers 503 instead of crashing.
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"
    DEEPSEEK_TIMEOUT: float = 45.0

    # --- n8n (lead search workflow) ----------------------------------------
    # Full URL of the webhook that kicks off the lead-search workflow. Use the
    # /webhook-test/ path while building on the canvas (it accepts one call per
    # "Execute workflow" click) and /webhook/ once the workflow is activated.
    # Empty disables dispatch: the endpoint then answers 503.
    N8N_WEBHOOK_URL: str = ""
    # Webhook of the SEPARATE workflow that handles Local Offline Business
    # searches (Google Maps data via the Local Business Data API). Empty
    # disables that search type only: the B2B search keeps working.
    N8N_LOCAL_WEBHOOK_URL: str = ""
    # How many listings the local workflow asks the API for, per query. The
    # API bills per result, so this caps what one search can cost upstream.
    LOCAL_SEARCH_RESULT_LIMIT: int = 60
    # Optional shared secret, sent as a header when set. Configure the matching
    # header auth on the n8n Webhook node so the endpoint is not wide open.
    N8N_WEBHOOK_SECRET: str = ""
    N8N_WEBHOOK_HEADER: str = "X-LeadPilot-Token"
    N8N_TIMEOUT: float = 30.0

    # Public base URL of THIS api, used to build the progress callback URL sent
    # to n8n. Must be reachable from the n8n host: "http://localhost:8000" only
    # works if n8n runs on the same machine.
    PUBLIC_API_URL: str = "http://localhost:8000"

    # Watchdog for runs the workflow never finished. n8n reports progress by
    # calling back; if it dies mid-run (or its Error Trigger cannot be tied to
    # our run) nothing else would ever close the row, and the user's tracker
    # would spin forever. A run with no checkpoint for STALE_MINUTES is failed;
    # a run whose last checkpoint said "completed" but whose results never
    # arrived within RESULTS_TIMEOUT_MINUTES is failed too (nothing was saved
    # and nothing is charged). 0 for the interval disables the sweeper.
    LEAD_SEARCH_STALE_MINUTES: int = 30
    LEAD_SEARCH_RESULTS_TIMEOUT_MINUTES: int = 15
    LEAD_SEARCH_SWEEP_INTERVAL_SECONDS: int = 60

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
