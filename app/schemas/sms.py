import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.outbound_message import ContactStage


class SmsSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_ids: list[uuid.UUID] = Field(min_length=1)
    message_template_id: uuid.UUID
    contact_type: ContactStage
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("lead_ids")
    @classmethod
    def unique_lead_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(value))

    @field_validator("idempotency_key")
    @classmethod
    def clean_idempotency_key(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(character.isspace() for character in cleaned):
            raise ValueError("idempotency_key cannot contain whitespace")
        return cleaned


class SmsRecipientResult(BaseModel):
    requested_lead_id: uuid.UUID
    lead_id: uuid.UUID | None
    message_id: uuid.UUID | None
    status: str
    provider_message_sid: str | None = None
    reason_code: str | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SmsBatchData(BaseModel):
    batch_id: uuid.UUID
    status: str
    selection_count: int
    contact_type: ContactStage
    accepted_count: int
    queued_count: int
    sent_count: int
    delivered_count: int
    failed_count: int
    undelivered_count: int
    unknown_count: int
    skipped_count: int
    replayed: bool = False
    recipients: list[SmsRecipientResult]


class SmsBatchResponse(BaseModel):
    success: bool = True
    message: str
    data: SmsBatchData


class SmsRecoveryData(BaseModel):
    idempotency_key: str
    found: bool
    operation_state: str
    batch: SmsBatchData | None = None


class SmsRecoveryResponse(BaseModel):
    success: bool = True
    message: str
    data: SmsRecoveryData


class SmsMessageEventData(BaseModel):
    status: str
    provider_error_code: str | None
    safe_reason: str | None
    occurred_at: datetime


class SmsMessageData(BaseModel):
    id: uuid.UUID
    batch_id: uuid.UUID
    lead_id: uuid.UUID
    message_template_id: uuid.UUID
    template_updated_at: datetime
    contact_type: ContactStage
    recipient_phone: str | None
    rendered_body: str
    status: str
    provider_message_sid: str | None
    provider_error_code: str | None
    failure_reason: str | None
    requested_by_user_id: uuid.UUID
    attempted_at: datetime | None
    accepted_at: datetime | None
    sent_at: datetime | None
    delivered_at: datetime | None
    failed_at: datetime | None
    last_status_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime
    events: list[SmsMessageEventData] = Field(default_factory=list)


class SmsMessageResponse(BaseModel):
    success: bool = True
    message: str | None = None
    refreshed: bool | None = None
    next_refresh_at: datetime | None = None
    data: SmsMessageData
