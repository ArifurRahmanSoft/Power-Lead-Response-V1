import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class CustomerDuplicateResolution(StrEnum):
    CREATE_NEW = "create_new"
    LINK_EXISTING = "link_existing"


class AcquisitionTaskDisposition(StrEnum):
    CANCEL = "cancel"
    RETAIN = "retain"


def _clean_required(value: object) -> object:
    if isinstance(value, str):
        return " ".join(value.strip().split())
    return value


def _clean_optional(value: object) -> object:
    if isinstance(value, str):
        return " ".join(value.strip().split()) or None
    return value


class LeadConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    purchased_service: str = Field(min_length=1, max_length=200)
    reference: str | None = Field(default=None, max_length=255)
    reason: str = Field(min_length=1, max_length=1000)
    duplicate_resolution: CustomerDuplicateResolution
    existing_customer_id: uuid.UUID | None = None
    pending_acquisition_task_disposition: AcquisitionTaskDisposition
    pending_task_reason: str = Field(min_length=1, max_length=1000)

    _normalize_required = field_validator(
        "purchased_service", "reason", "pending_task_reason", mode="before"
    )(_clean_required)
    _normalize_reference = field_validator("reference", mode="before")(_clean_optional)

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        if (
            self.duplicate_resolution == CustomerDuplicateResolution.LINK_EXISTING
            and self.existing_customer_id is None
        ):
            raise ValueError(
                "existing_customer_id is required when duplicate_resolution is link_existing"
            )
        if (
            self.duplicate_resolution == CustomerDuplicateResolution.CREATE_NEW
            and self.existing_customer_id is not None
        ):
            raise ValueError(
                "existing_customer_id is only allowed when duplicate_resolution is link_existing"
            )
        return self


class CustomerData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str
    last_name: str
    email: EmailStr
    phone: str | None
    company: str | None
    communication_purpose: str
    communication_consent_status: str
    channel_restrictions: list[str]
    suppressed_at: datetime | None
    suppression_reason: str | None
    created_at: datetime
    row_version: int

    @field_validator("suppressed_at", "created_at", mode="before")
    @classmethod
    def expose_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class LeadConversionData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    customer_id: uuid.UUID
    converted_at: datetime
    converted_by: uuid.UUID
    purchased_service: str
    reference: str | None
    reason: str
    duplicate_resolution: CustomerDuplicateResolution
    pending_task_disposition: AcquisitionTaskDisposition
    pending_task_reason: str
    cancelled_nurture_job_count: int
    affected_task_count: int
    source_consent_status: str
    source_consent_evidence: str | None
    source_consent_reference: str | None

    @field_validator("converted_at", mode="before")
    @classmethod
    def expose_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class LeadConversionResponseData(BaseModel):
    customer: CustomerData
    conversion: LeadConversionData
    idempotent_replay: bool


class LeadConversionResponse(BaseModel):
    success: bool
    message: str
    data: LeadConversionResponseData


class CustomerListData(BaseModel):
    items: list[CustomerData]
    page: int
    size: int
    total: int


class CustomerListResponse(BaseModel):
    success: bool
    data: CustomerListData


class CustomerDetailData(CustomerData):
    conversions: list[LeadConversionData]


class CustomerDetailResponse(BaseModel):
    success: bool
    data: CustomerDetailData
