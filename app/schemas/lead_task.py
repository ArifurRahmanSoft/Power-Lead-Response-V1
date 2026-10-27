import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.lead_activity import LeadActivityCreateRequest


class LeadTaskType(StrEnum):
    CONTACT = "contact"
    FOLLOW_UP = "follow_up"
    REVIEW = "review"
    OTHER = "other"


CONTACT_TASK_TYPES = frozenset({LeadTaskType.CONTACT, LeadTaskType.FOLLOW_UP})


class LeadTaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"

    @classmethod
    def _missing_(cls, value: object) -> "LeadTaskPriority | None":
        if isinstance(value, str) and value.strip().lower() == "medium":
            return cls.NORMAL
        return None


class LeadTaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


def _clean_required(value: object) -> object:
    if isinstance(value, str):
        value = " ".join(value.strip().split())
    return value


def _clean_optional(value: object) -> object:
    if isinstance(value, str):
        return value.strip() or None
    return value


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone offset")
    return value.astimezone(timezone.utc)


class LeadTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assigned_user_id: uuid.UUID
    task_type: LeadTaskType
    title: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=10_000)
    due_at: datetime
    priority: LeadTaskPriority = LeadTaskPriority.NORMAL
    related_activity_id: uuid.UUID | None = None

    _normalize_title = field_validator("title", mode="before")(_clean_required)
    _normalize_notes = field_validator("notes", mode="before")(_clean_optional)
    _normalize_due_at = field_validator("due_at")(_utc_datetime)


class LeadTaskUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    assigned_user_id: uuid.UUID | None = None
    task_type: LeadTaskType | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=10_000)
    due_at: datetime | None = None
    priority: LeadTaskPriority | None = None
    status: LeadTaskStatus | None = None
    related_activity_id: uuid.UUID | None = None

    _normalize_title = field_validator("title", mode="before")(_clean_required)
    _normalize_notes = field_validator("notes", mode="before")(_clean_optional)

    @field_validator("due_at")
    @classmethod
    def normalize_due_at(cls, value: datetime | None) -> datetime | None:
        return _utc_datetime(value) if value is not None else None

    @field_validator("status")
    @classmethod
    def restrict_status(cls, value: LeadTaskStatus | None) -> LeadTaskStatus | None:
        if value in {LeadTaskStatus.COMPLETED, LeadTaskStatus.CANCELLED}:
            raise ValueError("Use the dedicated complete or cancel endpoint")
        return value

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not (self.model_fields_set - {"row_version"}):
            raise ValueError("Provide at least one field to update")
        required_fields = {
            "assigned_user_id",
            "task_type",
            "title",
            "due_at",
            "priority",
            "status",
        }
        for field_name in required_fields & self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class LeadTaskTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: int = Field(ge=1)


class LeadTaskCompleteRequest(LeadTaskTransitionRequest):
    related_activity_id: uuid.UUID | None = None
    activity: LeadActivityCreateRequest | None = None

    @model_validator(mode="after")
    def one_activity_source(self) -> Self:
        if self.related_activity_id is not None and self.activity is not None:
            raise ValueError("Provide related_activity_id or activity, not both")
        return self


class LeadTaskData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    assigned_user_id: uuid.UUID
    task_type: LeadTaskType
    title: str
    notes: str | None
    due_at: datetime
    priority: LeadTaskPriority
    status: LeadTaskStatus
    related_activity_id: uuid.UUID | None
    completed_by: uuid.UUID | None
    completed_at: datetime | None
    cancelled_by: uuid.UUID | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    created_by: uuid.UUID
    created_at: datetime
    updated_by: uuid.UUID
    updated_at: datetime
    row_version: int

    @field_validator("due_at", "completed_at", "cancelled_at", "created_at", "updated_at", mode="before")
    @classmethod
    def expose_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class LeadTaskResponse(BaseModel):
    success: bool
    message: str | None = None
    data: LeadTaskData


class LeadTaskListData(BaseModel):
    items: list[LeadTaskData]
    page: int
    size: int
    total: int


class LeadTaskListResponse(BaseModel):
    success: bool
    data: LeadTaskListData


class LeadTaskListFilters(BaseModel):
    lead_id: uuid.UUID | None = None
    assigned_to_me: bool = False
    due_today: bool = False
    overdue: bool = False
    timezone_name: str = "UTC"
    status: LeadTaskStatus | None = None
    task_type: LeadTaskType | None = None
    priority: LeadTaskPriority | None = None

    @field_validator("timezone_name")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = value.strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exception:
            raise ValueError("timezone must be a valid IANA timezone name") from exception
        return value

    @model_validator(mode="after")
    def mutually_exclusive_due_filters(self) -> Self:
        if self.due_today and self.overdue:
            raise ValueError("due_today and overdue cannot both be true")
        return self
