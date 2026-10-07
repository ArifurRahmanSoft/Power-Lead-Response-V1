import uuid
from enum import StrEnum
from typing import Self

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from app.utils.identifiers import LOGIN_ID_PATTERN


class AuthStatus(StrEnum):
    WORKSPACE_SELECTED = "workspace_selected"
    WORKSPACE_SELECTION_REQUIRED = "workspace_selection_required"
    ONBOARDING_PENDING = "onboarding_pending"


class LoginRequest(BaseModel):
    identifier: str | None = Field(default=None, min_length=5, max_length=255)
    email: EmailStr | None = None
    password: str = Field(min_length=1, max_length=128)

    @field_validator("identifier", mode="before")
    @classmethod
    def trim_identifier(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "@" in value:
            try:
                return str(TypeAdapter(EmailStr).validate_python(value)).lower()
            except ValidationError as exception:
                raise ValueError("Identifier must be a valid email or login ID") from exception
        if not LOGIN_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "Login ID may contain only letters, numbers, dot, underscore, and hyphen"
            )
        return value.lower()

    @field_validator("email", mode="before")
    @classmethod
    def trim_legacy_email(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("email")
    @classmethod
    def normalize_legacy_email(cls, value: EmailStr | None) -> str | None:
        return str(value).lower() if value is not None else None

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be 72 bytes or fewer")
        return value

    @model_validator(mode="after")
    def validate_identifier_source(self) -> Self:
        if (self.identifier is None) == (self.email is None):
            raise ValueError("Provide identifier or legacy email, but not both")
        return self


class LoginUser(BaseModel):
    id: uuid.UUID
    login_id: str
    name: str
    email: EmailStr


class WorkspaceChoice(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: str


class SelectedWorkspace(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class AuthSessionData(BaseModel):
    auth_status: AuthStatus
    user: LoginUser
    selected_workspace: SelectedWorkspace | None = None
    role: str | None = None
    permissions: list[str] = Field(default_factory=list)
    workspaces: list[WorkspaceChoice] = Field(default_factory=list)


class LoginData(AuthSessionData):
    access_token: str
    token_type: str = "bearer"


class LoginResponse(BaseModel):
    success: bool
    message: str
    data: LoginData


class MeResponse(BaseModel):
    success: bool
    data: AuthSessionData


class WorkspaceSelectionRequest(BaseModel):
    workspace_id: uuid.UUID


class AuthenticationErrorResponse(BaseModel):
    success: bool
    message: str
