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
    smtp_credential_encryption_key: SecretStr | None = None
    smtp_credential_previous_keys: SecretStr | None = None
    smtp_connection_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    smtp_max_concurrent_operations: int = Field(default=5, ge=1, le=50)
    smtp_test_email_rate_limit_requests: int = Field(default=5, ge=1, le=50)
    smtp_test_email_rate_limit_window_seconds: int = Field(default=300, ge=1, le=3600)
    object_storage_bucket: str | None = None
    object_storage_region: str = "us-east-1"
    object_storage_endpoint_url: str | None = None
    object_storage_access_key_id: SecretStr | None = None
    object_storage_secret_access_key: SecretStr | None = None
    object_storage_use_ssl: bool = True
    mail_template_asset_max_file_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        le=25 * 1024 * 1024,
    )
    mail_template_max_assets: int = Field(default=20, ge=1, le=100)
    mail_template_max_total_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1024,
        le=50 * 1024 * 1024,
    )
    mail_preview_expire_minutes: int = Field(default=15, ge=1, le=120)
    mail_worker_poll_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    mail_worker_batch_size: int = Field(default=10, ge=1, le=100)
    mail_worker_concurrency: int = Field(default=4, ge=1, le=25)
    mail_worker_lease_seconds: int = Field(default=120, ge=30, le=1800)
    mail_worker_max_attempts: int = Field(default=4, ge=1, le=10)
    mail_worker_sender_rate_per_minute: int = Field(default=60, ge=1, le=10000)
    mail_worker_workspace_rate_per_minute: int = Field(default=300, ge=1, le=50000)
    mail_max_encoded_message_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1024,
        le=50 * 1024 * 1024,
    )
    mail_bulk_max_selection: int = Field(default=500, ge=1, le=5000)
    mail_bulk_preview_page_size: int = Field(default=50, ge=1, le=100)
    mail_direct_send_max_recipients: int = Field(default=10, ge=1, le=50)
    mail_direct_send_concurrency: int = Field(default=3, ge=1, le=10)
    mail_direct_send_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    twilio_account_sid: SecretStr | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_sms_from: str | None = None
    twilio_whatsapp_from: str | None = None
    twilio_whatsapp_webhook_base_url: str | None = None
    sms_direct_send_max_recipients: int = Field(default=10, ge=1, le=50)
    sms_direct_send_concurrency: int = Field(default=3, ge=1, le=10)
    sms_direct_send_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    sms_status_refresh_min_seconds: int = Field(default=30, ge=5, le=3600)
    whatsapp_reply_max_characters: int = Field(default=1600, ge=1, le=4096)
    whatsapp_reply_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    whatsapp_reply_rate_limit_requests: int = Field(default=30, ge=1, le=300)
    whatsapp_reply_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    meta_whatsapp_access_token: SecretStr | None = None
    meta_whatsapp_phone_number_id: str | None = None
    meta_whatsapp_waba_id: str | None = None
    meta_app_secret: SecretStr | None = None
    meta_webhook_verify_token: SecretStr | None = None
    meta_messenger_verify_token: SecretStr | None = None
    meta_messenger_page_id: str | None = None
    meta_messenger_page_access_token: SecretStr | None = None
    meta_messenger_app_secret: SecretStr | None = None
    meta_messenger_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    meta_messenger_reply_max_characters: int = Field(default=2000, ge=1, le=2000)
    meta_messenger_reply_rate_limit_requests: int = Field(default=30, ge=1, le=300)
    meta_messenger_reply_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    meta_messenger_profile_cache_hours: int = Field(default=24, ge=1, le=720)
    meta_messenger_profile_retry_seconds: int = Field(default=900, ge=60, le=86400)
    meta_messenger_profile_refresh_limit: int = Field(default=25, ge=1, le=100)
    meta_messenger_profile_refresh_concurrency: int = Field(default=4, ge=1, le=10)
    meta_messenger_profile_refresh_rate_limit_requests: int = Field(default=5, ge=1, le=60)
    meta_messenger_profile_refresh_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    meta_instagram_config_id: str | None = None
    meta_instagram_app_id: str | None = None
    meta_instagram_account_id: str | None = None
    meta_instagram_access_token: SecretStr | None = None
    meta_instagram_verify_token: SecretStr | None = None
    meta_instagram_app_secret: SecretStr | None = None
    meta_instagram_oauth_redirect_uri: str | None = None
    meta_instagram_oauth_state_expire_minutes: int = Field(default=10, ge=5, le=30)
    meta_instagram_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    meta_instagram_reply_rate_limit_requests: int = Field(default=30, ge=1, le=300)
    meta_instagram_reply_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    meta_graph_api_version: str = "v26.0"
    meta_whatsapp_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    meta_whatsapp_send_rate_limit_requests: int = Field(default=30, ge=1, le=300)
    meta_whatsapp_send_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    meta_whatsapp_template_fetch_limit: int = Field(default=500, ge=1, le=2000)

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

    @property
    def whatsapp_webhook_base_url(self) -> str | None:
        value = (self.twilio_whatsapp_webhook_base_url or "").strip().rstrip("/")
        return value or None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

