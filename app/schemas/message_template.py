import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _clean_name(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        raise ValueError("Template name is required")
    if len(cleaned) > 200:
        raise ValueError("Template name must be at most 200 characters")
    return cleaned


def _validate_body(value: str) -> str:
    if not value.strip():
        raise ValueError("Template body is required")
    if len(value) > 10_000:
        raise ValueError("Template body must be at most 10000 characters")
    return value


class MessageTemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    body: str

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str) -> str:
        return _validate_body(value)


class MessageTemplateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    body: str | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return _clean_name(value) if value is not None else None

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str | None) -> str | None:
        return _validate_body(value) if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> "MessageTemplateUpdateRequest":
        if self.name is None and self.body is None:
            raise ValueError("At least one field is required")
        return self


class MessageTemplatePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: uuid.UUID | None = None
    sample_values: dict[str, str | None] | None = None

    @model_validator(mode="after")
    def one_data_source(self) -> "MessageTemplatePreviewRequest":
        if self.lead_id is not None and self.sample_values is not None:
            raise ValueError("Use either lead_id or sample_values, not both")
        if self.sample_values is not None:
            if len(self.sample_values) > 30:
                raise ValueError("Too many sample values")
            for value in self.sample_values.values():
                if value is not None and len(value) > 2000:
                    raise ValueError("Sample values must be at most 2000 characters")
        return self


class MessageTemplateData(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    category: str = "message"
    name: str
    body: str
    archived: bool
    created_by_user_id: uuid.UUID
    updated_by_user_id: uuid.UUID
    archived_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class MessageTemplateListData(BaseModel):
    items: list[MessageTemplateData]
    page: int
    size: int
    total: int


class MessageTemplateResponse(BaseModel):
    success: bool = True
    message: str | None = None
    data: MessageTemplateData


class MessageTemplateListResponse(BaseModel):
    success: bool = True
    data: MessageTemplateListData


class MessageTemplateActionResponse(BaseModel):
    success: bool = True
    message: str


class MessageTemplateRenderIssue(BaseModel):
    placeholder: str
    message: str


class MessageTemplatePreviewData(BaseModel):
    template_id: uuid.UUID
    rendered_body: str
    missing_values: list[MessageTemplateRenderIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MessageTemplatePreviewResponse(BaseModel):
    success: bool = True
    data: MessageTemplatePreviewData


class MessagePlaceholderDefinition(BaseModel):
    key: str
    label: str


class MessagePlaceholderCatalogResponse(BaseModel):
    success: bool = True
    data: list[MessagePlaceholderDefinition]
