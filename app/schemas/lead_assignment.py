import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_validator


class LeadAssignmentHistoryData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    previous_assignee_id: uuid.UUID | None
    new_assignee_id: uuid.UUID | None
    actor_user_id: uuid.UUID
    assigned_at: datetime
    reason: str
    task_disposition: str
    affected_task_count: int
    lead_row_version: int

    @field_validator("assigned_at", mode="before")
    @classmethod
    def expose_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class LeadAssignmentHistoryListData(BaseModel):
    items: list[LeadAssignmentHistoryData]
    page: int
    size: int
    total: int


class LeadAssignmentHistoryListResponse(BaseModel):
    success: bool
    data: LeadAssignmentHistoryListData
