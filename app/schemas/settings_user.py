import uuid
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.utils.identifiers import LOGIN_ID_PATTERN


class UserSetupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    login_id: str = Field(min_length=5, max_length=50)
    password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)
    role_id: uuid.UUID

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError("Display name is required")
        return cleaned

    @field_validator("email", mode="before")
    @classmethod
    def clean_email(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("login_id")
    @classmethod
    def clean_login_id(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not LOGIN_ID_PATTERN.fullmatch(normalized):
            raise ValueError(
                "Login ID may contain only letters, numbers, dot, underscore, and hyphen"
            )
        return normalized

    @field_validator("password", "confirm_password")
    @classmethod
    def validate_password_length(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be 72 bytes or fewer")
        return value

    @model_validator(mode="after")
    def validate_password_match(self) -> Self:
        if self.password != self.confirm_password:
            raise ValueError("Password and confirm password must match")
        return self


class UserSetupUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    role_id: uuid.UUID | None = None

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError("Display name is required")
        return cleaned

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if self.display_name is None and self.role_id is None:
            raise ValueError("Provide display_name or role_id")
        return self


class AdminPasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)

    @field_validator("password", "confirm_password")
    @classmethod
    def validate_password_length(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be 72 bytes or fewer")
        return value

    @model_validator(mode="after")
    def validate_match(self) -> Self:
        if self.password != self.confirm_password:
            raise ValueError("Password and confirm password must match")
        return self


class UserSetupData(BaseModel):
    id: uuid.UUID
    membership_id: uuid.UUID
    display_name: str
    email: EmailStr
    login_id: str
    role_id: uuid.UUID
    role_name: str
    is_active: bool


class UserSetupListData(BaseModel):
    items: list[UserSetupData]
    page: int
    size: int
    total: int


class UserSetupListResponse(BaseModel):
    success: bool
    data: UserSetupListData


class UserSetupResponse(BaseModel):
    success: bool
    message: str
    data: UserSetupData


class UserSetupActionResponse(BaseModel):
    success: bool
    message: str
