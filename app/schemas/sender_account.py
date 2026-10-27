import uuid
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from app.utils.smtp_security import normalize_smtp_host, validate_smtp_port

SenderProvider = Literal["gmail", "custom_smtp"]
AuthenticationType = Literal["app_password", "password"]
EncryptionType = Literal["STARTTLS", "TLS"]


def _clean_required(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        raise ValueError("Field is required")
    return cleaned


class SenderAccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    sender_email: EmailStr
    provider: SenderProvider
    authentication_type: AuthenticationType
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = None
    encryption: EncryptionType = "STARTTLS"
    smtp_username: str | None = Field(default=None, max_length=255)
    credential: SecretStr
    reply_to: EmailStr | None = None
    is_active: bool = True
    is_default: bool = False

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value: str) -> str:
        return _clean_required(value)

    @field_validator("sender_email", "reply_to", mode="before")
    @classmethod
    def clean_email(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return str(value).strip().lower()

    @field_validator("smtp_host")
    @classmethod
    def clean_host(cls, value: str | None) -> str | None:
        return normalize_smtp_host(value) if value else None

    @field_validator("smtp_port")
    @classmethod
    def clean_port(cls, value: int | None) -> int | None:
        return validate_smtp_port(value) if value is not None else None

    @field_validator("smtp_username")
    @classmethod
    def clean_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("credential")
    @classmethod
    def credential_is_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("Credential is required")
        return value

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> "SenderAccountCreateRequest":
        if self.provider == "gmail":
            if self.authentication_type != "app_password":
                raise ValueError("Gmail requires authentication_type app_password")
            self.smtp_host = self.smtp_host or "smtp.gmail.com"
            if self.smtp_host != "smtp.gmail.com":
                raise ValueError("Gmail provider must use smtp.gmail.com")
            self.smtp_port = self.smtp_port or (465 if self.encryption == "TLS" else 587)
            self.smtp_username = self.smtp_username or str(self.sender_email)
        else:
            if self.authentication_type != "password":
                raise ValueError("Custom SMTP supports authentication_type password")
            if self.smtp_host is None or self.smtp_port is None or self.smtp_username is None:
                raise ValueError(
                    "Custom SMTP requires smtp_host, smtp_port, and smtp_username"
                )
        if self.is_default and not self.is_active:
            raise ValueError("The default sender must be active")
        return self


class SenderAccountUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    sender_email: EmailStr | None = None
    provider: SenderProvider | None = None
    authentication_type: AuthenticationType | None = None
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = None
    encryption: EncryptionType | None = None
    smtp_username: str | None = Field(default=None, max_length=255)
    credential: SecretStr | None = None
    reply_to: EmailStr | None = None
    is_active: bool | None = None

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value: str | None) -> str | None:
        return _clean_required(value) if value is not None else None

    @field_validator("sender_email", "reply_to", mode="before")
    @classmethod
    def clean_email(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return str(value).strip().lower()

    @field_validator("smtp_host")
    @classmethod
    def clean_host(cls, value: str | None) -> str | None:
        return normalize_smtp_host(value) if value else None

    @field_validator("smtp_port")
    @classmethod
    def clean_port(cls, value: int | None) -> int | None:
        return validate_smtp_port(value) if value is not None else None

    @field_validator("smtp_username")
    @classmethod
    def clean_username(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("credential")
    @classmethod
    def credential_is_not_blank(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            raise ValueError("Credential cannot be blank")
        return value


class SenderTestEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: EmailStr

    @field_validator("recipient", mode="before")
    @classmethod
    def clean_recipient(cls, value: object) -> object:
        return str(value).strip().lower()


class SenderAccountData(BaseModel):
    id: uuid.UUID
    display_name: str
    sender_email: EmailStr
    provider: SenderProvider
    authentication_type: AuthenticationType
    smtp_host: str
    smtp_port: int
    encryption: EncryptionType
    smtp_username: str
    credentials_configured: bool
    reply_to: EmailStr | None
    is_active: bool
    is_default: bool
    connection_status: Literal["not_tested", "success", "failed"]
    last_tested_at: datetime | None
    last_test_message: str | None
    created_at: datetime
    updated_at: datetime


class SenderAccountListData(BaseModel):
    items: list[SenderAccountData]
    page: int
    size: int
    total: int


class SenderAccountResponse(BaseModel):
    success: bool
    message: str | None = None
    data: SenderAccountData


class SenderAccountListResponse(BaseModel):
    success: bool
    data: SenderAccountListData


class SenderActionResponse(BaseModel):
    success: bool
    message: str


class SenderConnectionTestData(BaseModel):
    sender_account_id: uuid.UUID
    connection_status: Literal["success", "failed"]
    last_tested_at: datetime


class SenderConnectionTestResponse(BaseModel):
    success: bool
    message: str
    data: SenderConnectionTestData
