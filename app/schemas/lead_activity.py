import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LeadActivityType(StrEnum):
    INITIAL_CONTACT = "initial_contact"
    FOLLOW_UP = "follow_up"
    INBOUND_REPLY = "inbound_reply"
    NOTE = "note"


class LeadActivityChannel(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    OTHER = "other"


class LeadActivityDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"
    INTERNAL = "internal"


class LeadActivityValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity_type: LeadActivityType
    channel: LeadActivityChannel
    direction: LeadActivityDirection
    occurred_at: datetime
    performed_by: uuid.UUID | None = None
    outcome: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=10_000)

    @field_validator("outcome", "notes", mode="before")
    @classmethod
    def clean_optional_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @field_validator("occurred_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_activity_semantics(self) -> Self:
        expected_direction = {
            LeadActivityType.INITIAL_CONTACT: LeadActivityDirection.OUTBOUND,
            LeadActivityType.FOLLOW_UP: LeadActivityDirection.OUTBOUND,
            LeadActivityType.INBOUND_REPLY: LeadActivityDirection.INBOUND,
            LeadActivityType.NOTE: LeadActivityDirection.INTERNAL,
        }[self.activity_type]
        if self.direction != expected_direction:
            raise ValueError(
                f"{self.activity_type.value} activities must use {expected_direction.value} direction"
            )
        if self.activity_type == LeadActivityType.NOTE:
            if self.channel != LeadActivityChannel.OTHER:
                raise ValueError("note activities must use the other channel")
            if not self.notes:
                raise ValueError("note activities require notes")
        return self


class LeadActivityCreateRequest(LeadActivityValues):
    related_activity_id: uuid.UUID | None = None


class LeadActivityAmendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amendment_reason: str = Field(min_length=1, max_length=1000)
    activity_type: LeadActivityType | None = None
    channel: LeadActivityChannel | None = None
    direction: LeadActivityDirection | None = None
    occurred_at: datetime | None = None
    performed_by: uuid.UUID | None = None
    outcome: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=10_000)

    @field_validator("amendment_reason", mode="before")
    @classmethod
    def clean_reason(cls, value: object) -> object:
        if isinstance(value, str):
            value = " ".join(value.strip().split())
        return value

    @field_validator("outcome", "notes", mode="before")
    @classmethod
    def clean_optional_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @field_validator("occurred_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone offset")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_correction(self) -> Self:
        if not (self.model_fields_set - {"amendment_reason"}):
            raise ValueError("Provide at least one corrected field")
        return self


class LeadActivityData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    activity_type: LeadActivityType
    channel: LeadActivityChannel
    direction: LeadActivityDirection
    occurred_at: datetime
    performed_by: uuid.UUID | None
    outcome: str | None
    notes: str | None
    related_activity_id: uuid.UUID | None
    recorded_by: uuid.UUID
    recorded_at: datetime
    is_system: bool
    is_amendment: bool
    externally_performed: bool
    provider_verified: bool
    amendment_reason: str | None
    outbound_message_id: uuid.UUID | None = None
    contact_stage: str | None = None
    message_status: str | None = None
    message_events: list[dict[str, object]] = []
    email_delivery_verified: bool = False

    @field_validator("occurred_at", "recorded_at", mode="before")
    @classmethod
    def expose_utc(cls, value: datetime) -> datetime:
        # PostgreSQL retains timezone awareness. SQLite, used by the isolated
        # test suite, may return an equivalent naive UTC value.
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class LeadActivityResponse(BaseModel):
    success: bool
    message: str | None = None
    data: LeadActivityData


class LeadActivityListData(BaseModel):
    items: list[LeadActivityData]
    page: int
    size: int
    total: int


class LeadActivityListResponse(BaseModel):
    success: bool
    data: LeadActivityListData
