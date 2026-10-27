"""Application settings, loaded from environment variables (and .env when present).

Every setting the app reads lives here, so .env.example and this file are the
single place to check what the deployment needs.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: SecretStr = SecretStr("")
    # Kept as a raw string: pydantic-settings would otherwise expect JSON for a list.
    allowed_user_ids_raw: str = Field(default="", alias="ALLOWED_USER_IDS")

    # Owner
    owner_telegram_id: int | None = None
    owner_name: str = "Owner"
    owner_email: str | None = None

    # Email (Resend)
    resend_api_key: SecretStr = SecretStr("")
    email_from: str = ""

    # PostgreSQL
    postgres_user: str = "assistant"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "assistant"
    postgres_host: str = "db"
    postgres_port: int = 5432

    # App
    default_timezone: str = "Asia/Tehran"
    voice_storage_dir: Path = Path("/data/voice")
    log_level: str = "INFO"

    # Speech to text
    vosk_model_path: Path = Path("/opt/vosk-model")
    # Mean word confidence below this asks the user to repeat.
    stt_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    # Longer voices are refused before download: bounds CPU time per message.
    stt_max_duration_s: int = Field(default=120, gt=0)

    # LLM (DeepSeek, OpenAI-compatible API)
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    llm_timeout_s: float = Field(default=30.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0, le=6)
    agent_max_steps: int = Field(default=6, ge=1, le=6)
    # A preview older than this can no longer be confirmed ("tomorrow" would be stale).
    confirmation_ttl_min: int = Field(default=60, gt=0)

    # Google Calendar (OAuth 2.0, Desktop app client; see scripts/google_auth.py)
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_refresh_token: SecretStr = SecretStr("")
    google_calendar_id: str = "primary"

    @property
    def google_calendar_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret.get_secret_value()
                    and self.google_refresh_token.get_secret_value())

    @field_validator("allowed_user_ids_raw")
    @classmethod
    def _validate_ids(cls, v: str) -> str:
        for part in v.split(","):
            part = part.strip()
            if part and not part.lstrip("-").isdigit():
                raise ValueError(f"ALLOWED_USER_IDS must be comma-separated integers, got {part!r}")
        return v

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        return frozenset(int(p) for p in self.allowed_user_ids_raw.split(",") if p.strip())

    def database_url(self, driver: str = "psycopg") -> str:
        """SQLAlchemy URL. psycopg 3 serves both sync (Alembic) and async (app) engines."""
        from sqlalchemy.engine import URL

        return URL.create(
            drivername=f"postgresql+{driver}",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
