import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MailTemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)
    subject: str = Field(min_length=1, max_length=255)
    body_html: str = Field(min_length=1, max_length=200_000)
    signature_html: str = Field(default="", max_length=100_000)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.strip().split())
        if len(cleaned) < 2:
            raise ValueError("Template name must be at least 2 characters")
        return cleaned


class MailTemplateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=120)
    subject: str | None = Field(default=None, min_length=1, max_length=255)
    body_html: str | None = Field(default=None, min_length=1, max_length=200_000)
    signature_html: str | None = Field(default=None, max_length=100_000)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.strip().split())
        if len(cleaned) < 2:
            raise ValueError("Template name must be at least 2 characters")
        return cleaned


class MailTemplatePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: uuid.UUID | None = None
    sample_values: dict[str, str | None] | None = None
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def one_data_source(self) -> "MailTemplatePreviewRequest":
        if self.lead_id is not None and self.sample_values is not None:
            raise ValueError("Use either lead_id or sample_values, not both")
        if self.sample_values is not None:
            if len(self.sample_values) > 30:
                raise ValueError("Too many sample values")
            for value in self.sample_values.values():
                if value is not None and len(value) > 2000:
                    raise ValueError("Sample values must be at most 2000 characters")
        return self


class MailTemplateAssetData(BaseModel):
    id: uuid.UUID
    kind: Literal["inline_body", "signature_image", "attachment"]
    filename: str
    content_type: str
    size_bytes: int
    placeholder_html: str | None = None
    content_url: str
    created_at: datetime


class MailTemplateData(BaseModel):
    id: uuid.UUID
    name: str
    enabled: bool
    archived: bool
    version_id: uuid.UUID
    version: int
    approval_status: Literal["draft", "approved"]
    subject: str
    body_html: str
    body_text: str
    signature_html: str
    signature_text: str
    assets: list[MailTemplateAssetData]
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version_created_at: datetime
    version_updated_at: datetime


class MailTemplateListItem(BaseModel):
    id: uuid.UUID
    name: str
    enabled: bool
    archived: bool
    version: int
    approval_status: Literal["draft", "approved"]
    subject: str
    updated_at: datetime


class MailTemplateListData(BaseModel):
    items: list[MailTemplateListItem]
    page: int
    size: int
    total: int


class MailTemplateResponse(BaseModel):
    success: bool
    message: str | None = None
    data: MailTemplateData


class MailTemplateListResponse(BaseModel):
    success: bool
    data: MailTemplateListData


class MailTemplateActionResponse(BaseModel):
    success: bool
    message: str


class TemplateRenderIssue(BaseModel):
    placeholder: str
    message: str


class MailTemplatePreviewData(BaseModel):
    template_id: uuid.UUID
    version: int
    rendered_subject: str
    rendered_body_html: str
    rendered_signature_html: str
    rendered_html: str
    rendered_text: str
    missing_values: list[TemplateRenderIssue]
    warnings: list[str]
    asset_urls: dict[str, str]


class MailTemplatePreviewResponse(BaseModel):
    success: bool
    data: MailTemplatePreviewData


class PlaceholderDefinition(BaseModel):
    key: str
    label: str


class PlaceholderCatalogResponse(BaseModel):
    success: bool
    data: list[PlaceholderDefinition]


class AssetUploadResponse(BaseModel):
    success: bool
    message: str
    data: MailTemplateAssetData
