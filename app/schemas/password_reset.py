from typing import Self

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class ForgotPasswordRequest(BaseModel):
    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def trim_email(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).lower()


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=255)
    new_password: str = Field(min_length=1, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        cleaned_value = value.strip()
        if not cleaned_value:
            raise ValueError("Token is required")
        return cleaned_value

    @field_validator("new_password", "confirm_password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Password is required")
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be 72 bytes or fewer")
        return value

    @model_validator(mode="after")
    def validate_matching_passwords(self) -> Self:
        if self.new_password != self.confirm_password:
            raise ValueError("New password and confirm password must match")
        return self


class PasswordResetResponse(BaseModel):
    success: bool
    message: str


class PasswordResetErrorResponse(BaseModel):
    success: bool
    message: str
