import uuid
from datetime import datetime, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.lead_activity import LeadActivityChannel


def _utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone offset")
    return value.astimezone(timezone.utc)


def _optional_text(value: object) -> object:
    if isinstance(value, str):
        return value.strip() or None
    return value


class ManualReplyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: LeadActivityChannel
    occurred_at: datetime
    summary: str = Field(min_length=1, max_length=10_000)
    related_outbound_activity_id: uuid.UUID

    _normalize_occurred_at = field_validator("occurred_at")(_utc_timestamp)

    @field_validator("summary", mode="before")
    @classmethod
    def clean_summary(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class ManualReplyData(BaseModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    channel: LeadActivityChannel
    occurred_at: datetime
    summary: str
    related_outbound_activity_id: uuid.UUID
    recorded_by: uuid.UUID
    recorded_at: datetime
    provider_verified: bool

    @field_validator("occurred_at", "recorded_at", mode="before")
    @classmethod
    def expose_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class ManualReplyResponse(BaseModel):
    success: bool
    message: str
    data: ManualReplyData


class BookingSource(StrEnum):
    MANUAL = "manual"
    EMAIL = "email"
    PHONE = "phone"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    OTHER = "other"


class BookingStatus(StrEnum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class BookingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_at: datetime
    timezone: str
    responsible_user_id: uuid.UUID
    source: BookingSource = BookingSource.MANUAL
    status: BookingStatus = BookingStatus.SCHEDULED
    notes: str | None = Field(default=None, max_length=10_000)

    _normalize_scheduled_at = field_validator("scheduled_at")(_utc_timestamp)
    _normalize_notes = field_validator("notes", mode="before")(_optional_text)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exception:
            raise ValueError("timezone must be a valid IANA timezone name") from exception
        return value

    @field_validator("status")
    @classmethod
    def initial_status(cls, value: BookingStatus) -> BookingStatus:
        if value not in {BookingStatus.SCHEDULED, BookingStatus.CONFIRMED}:
            raise ValueError("New bookings must be scheduled or confirmed")
        return value


class BookingTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: int = Field(ge=1)
    notes: str | None = Field(default=None, max_length=10_000)

    _normalize_notes = field_validator("notes", mode="before")(_optional_text)


class BookingRescheduleRequest(BookingTransitionRequest):
    scheduled_at: datetime
    timezone: str

    _normalize_scheduled_at = field_validator("scheduled_at")(_utc_timestamp)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exception:
            raise ValueError("timezone must be a valid IANA timezone name") from exception
        return value


class BookingData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    scheduled_at: datetime
    timezone: str
    responsible_user_id: uuid.UUID
    source: BookingSource
    status: BookingStatus
    notes: str | None
    provider_verified: bool
    created_by: uuid.UUID
    created_at: datetime
    updated_by: uuid.UUID
    updated_at: datetime
    row_version: int

    @field_validator("scheduled_at", "created_at", "updated_at", mode="before")
    @classmethod
    def expose_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class BookingResponse(BaseModel):
    success: bool
    message: str | None = None
    data: BookingData


class BookingListData(BaseModel):
    items: list[BookingData]
    page: int
    size: int
    total: int


class BookingListResponse(BaseModel):
    success: bool
    data: BookingListData


class BookingEventData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    previous_status: BookingStatus | None
    new_status: BookingStatus
    previous_scheduled_at: datetime | None
    new_scheduled_at: datetime
    previous_timezone: str | None
    new_timezone: str
    notes: str | None
    recorded_by: uuid.UUID
    recorded_at: datetime

    @field_validator(
        "previous_scheduled_at", "new_scheduled_at", "recorded_at", mode="before"
    )
    @classmethod
    def expose_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class BookingHistoryData(BaseModel):
    items: list[BookingEventData]
    page: int
    size: int
    total: int


class BookingHistoryResponse(BaseModel):
    success: bool
    data: BookingHistoryData
