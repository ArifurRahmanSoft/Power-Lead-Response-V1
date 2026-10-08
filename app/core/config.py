from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]

# Default environment selector. It can be overridden with LIVE=true in .env.
LIVE = True
LOCAL_FRONTEND_URL = "http://localhost:4200"
PRODUCTION_FRONTEND_URL = "https://powerleadresponse.netlify.app"


class Settings(BaseSettings):
    live: bool = LIVE
    local_database_url: str
    production_database_url: str
    jwt_secret_key: SecretStr = Field(min_length=32)
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = Field(default=30, gt=0)
    password_reset_token_expire_minutes: int = Field(default=15, gt=0, le=60)
    password_reset_path: str = "/reset-password"
    lead_import_max_file_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    lead_import_max_rows: int = Field(default=5000, ge=1, le=50000)
    lead_import_retention_hours: int = Field(default=72, ge=1, le=720)
    groq_api_key: SecretStr | None = None
    groq_transcription_model: str = "whisper-large-v3-turbo"
    groq_extraction_model: str = "openai/gpt-oss-20b"
    groq_request_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    lead_voice_max_upload_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        le=25 * 1024 * 1024,
    )
    lead_voice_max_duration_seconds: float = Field(default=120.0, ge=1.0, le=1800.0)
    lead_voice_rate_limit_requests: int = Field(default=10, ge=1, le=100)
    lead_voice_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        selected_url = (
            self.production_database_url if self.live else self.local_database_url
        )

        # Explicitly select psycopg 3 while accepting the requested postgresql:// form.
        if selected_url.startswith("postgresql://"):
            return selected_url.replace("postgresql://", "postgresql+psycopg://", 1)

        return selected_url

    @property
    def frontend_url(self) -> str:
        return PRODUCTION_FRONTEND_URL if self.live else LOCAL_FRONTEND_URL


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

