import uuid
from typing import Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class UserRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    login_id: str | None = None
    password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned_value = value.strip()
        if not cleaned_value:
            raise ValueError("Name is required")
        return cleaned_value

    @field_validator("email", mode="before")
    @classmethod
    def trim_email(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).lower()

    @field_validator("login_id")
    @classmethod
    def validate_login_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not 5 <= len(normalized) <= 50:
            raise ValueError("Login ID must be between 5 and 50 characters")
        if not all(character.isalnum() or character in "._-" for character in normalized):
            raise ValueError(
                "Login ID may contain only letters, numbers, dot, underscore, and hyphen"
            )
        return normalized

    @field_validator("password", "confirm_password")
    @classmethod
    def validate_bcrypt_length(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be 72 bytes or fewer")
        return value

    @model_validator(mode="after")
    def validate_matching_passwords(self) -> Self:
        if self.password != self.confirm_password:
            raise ValueError("Password and confirm password must match")
        return self


class RegisteredUser(BaseModel):
    id: uuid.UUID
    name: str
    email: EmailStr
    login_id: str


class RegistrationResponse(BaseModel):
    success: bool
    message: str
    data: RegisteredUser


class ErrorResponse(BaseModel):
    success: bool
    message: str

