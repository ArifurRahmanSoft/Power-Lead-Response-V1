import uuid
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from app.schemas.lead import LeadFilterParams


class MailContextType(StrEnum):
    LEAD = "lead"
    CUSTOMER = "customer"


class CommunicationPurpose(StrEnum):
    LEAD_ACQUISITION = "lead_acquisition"
    CUSTOMER_SERVICE = "customer_service"


class ContactStage(StrEnum):
    INITIAL = "initial"
    FOLLOW_UP = "follow_up"
    SECOND_FOLLOW_UP = "second_follow_up"
    FINAL_FOLLOW_UP = "final_follow_up"


class OutboundPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_type: MailContextType
    context_id: uuid.UUID
    sender_account_id: uuid.UUID
    template_id: uuid.UUID
    template_version: int = Field(ge=1)
    purpose: CommunicationPurpose

    @model_validator(mode="after")
    def validate_purpose(self) -> "OutboundPreviewRequest":
        expected = (
            CommunicationPurpose.LEAD_ACQUISITION
            if self.context_type == MailContextType.LEAD
            else CommunicationPurpose.CUSTOMER_SERVICE
        )
        if self.purpose != expected:
            raise ValueError(f"{self.context_type.value} context requires {expected.value}")
        return self


class OutboundSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: uuid.UUID
    preview_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def clean_key(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(character.isspace() for character in cleaned):
            raise ValueError("idempotency_key cannot contain whitespace")
        return cleaned


class SenderSnapshotData(BaseModel):
    display_name: str
    sender_email: EmailStr
    reply_to: EmailStr | None


class OutboundPreviewData(BaseModel):
    preview_id: uuid.UUID
    preview_hash: str
    expires_at: datetime
    context_type: MailContextType
    context_id: uuid.UUID
    recipient: EmailStr
    purpose: CommunicationPurpose
    sender: SenderSnapshotData
    template_id: uuid.UUID
    template_version: int
    subject: str
    body_html: str
    body_text: str
    attachment_count: int
    inline_asset_count: int


class OutboundPreviewResponse(BaseModel):
    success: bool = True
    data: OutboundPreviewData


class MessageEventData(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    status: str
    safe_reason: str | None
    occurred_at: datetime

    @field_validator("occurred_at", mode="before")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class OutboundMessageData(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    # Direct SMTP sends do not create an outbox job. Worker-backed messages
    # continue to expose their durable job identifier.
    job_id: uuid.UUID | None = None
    context_type: MailContextType
    lead_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    purpose: CommunicationPurpose
    batch_id: uuid.UUID | None = None
    contact_stage: ContactStage = ContactStage.INITIAL
    recipient_email: EmailStr
    sender: SenderSnapshotData
    template_id: uuid.UUID
    template_version: int
    subject: str
    status: str
    created_at: datetime
    processing_at: datetime | None
    sent_at: datetime | None
    failed_at: datetime | None
    cancelled_at: datetime | None
    failure_reason: str | None
    events: list[MessageEventData] = []


class OutboundMessageResponse(BaseModel):
    success: bool = True
    message: str | None = None
    data: OutboundMessageData


class OutboundMessageListData(BaseModel):
    items: list[OutboundMessageData]
    page: int
    size: int
    total: int


class OutboundMessageListResponse(BaseModel):
    success: bool = True
    data: OutboundMessageListData


class OutboundCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=2, max_length=500)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        return " ".join(value.strip().split())


class BulkPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection_mode: str = Field(pattern="^(selected|all_matching)$")
    lead_ids: list[uuid.UUID] | None = None
    filters: LeadFilterParams | None = None
    sender_account_id: uuid.UUID
    template_id: uuid.UUID
    template_version: int = Field(ge=1)
    contact_stage: ContactStage
    page: int = Field(default=1, ge=1)
    size: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def validate_selection(self) -> "BulkPreviewRequest":
        if self.selection_mode == "selected":
            if not self.lead_ids:
                raise ValueError("lead_ids are required for selected mode")
            if self.filters is not None:
                raise ValueError("filters are only allowed for all_matching mode")
        else:
            if self.lead_ids is not None:
                raise ValueError("lead_ids are only allowed for selected mode")
            if self.filters is None:
                self.filters = LeadFilterParams()
        return self


class DirectBatchSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection_mode: str = Field(pattern="^(selected|all_matching)$")
    lead_ids: list[uuid.UUID] | None = None
    filters: LeadFilterParams | None = None
    sender_account_id: uuid.UUID
    template_id: uuid.UUID
    template_version: int = Field(ge=1)
    contact_type: ContactStage
    supersede_queued: bool = False
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def clean_idempotency_key(cls, value: str) -> str:
        cleaned = value.strip()
        if any(character.isspace() for character in cleaned):
            raise ValueError("idempotency_key cannot contain whitespace")
        return cleaned

    @model_validator(mode="after")
    def validate_selection(self) -> "DirectBatchSendRequest":
        if self.selection_mode == "selected":
            if not self.lead_ids:
                raise ValueError("lead_ids are required for selected mode")
            if self.filters is not None:
                raise ValueError("filters are only allowed for all_matching mode")
        else:
            if self.lead_ids is not None:
                raise ValueError("lead_ids are only allowed for selected mode")
            if self.filters is None:
                self.filters = LeadFilterParams()
        return self


class DirectBatchRecipientData(BaseModel):
    requested_lead_id: uuid.UUID
    lead_id: uuid.UUID | None
    message_id: uuid.UUID | None
    status: str
    skip_code: str | None = None
    skip_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class DirectBatchSendData(BaseModel):
    batch_id: uuid.UUID
    status: str
    selection_count: int
    queued_count: int
    processing_count: int
    sent_count: int
    failed_count: int
    unknown_count: int
    skipped_count: int
    pending_worker_job_count: int
    contact_type: ContactStage
    replayed: bool = False
    recipients: list[DirectBatchRecipientData]


class DirectBatchSendResponse(BaseModel):
    success: bool = True
    message: str
    data: DirectBatchSendData


class BulkRecipientData(BaseModel):
    id: uuid.UUID
    lead_id: uuid.UUID | None
    requested_lead_id: uuid.UUID
    recipient_email: EmailStr | None
    preview_status: str
    exclusion_code: str | None
    exclusion_reason: str | None
    warnings: list[str]
    preview_id: uuid.UUID | None
    message_id: uuid.UUID | None
    message_status: str | None = None
    subject: str | None = None
    body_html: str | None = None
    body_text: str | None = None
    events: list[MessageEventData] = []


class BulkBatchData(BaseModel):
    id: uuid.UUID
    status: str
    selection_mode: str
    selection_count: int
    eligible_count: int
    blocked_count: int
    preview_hash: str
    expires_at: datetime
    sender: SenderSnapshotData
    template_id: uuid.UUID
    template_version: int
    contact_stage: ContactStage
    queued_count: int = 0
    sent_count: int = 0
    failed_count: int = 0
    unknown_count: int = 0
    cancelled_count: int = 0
    skipped_count: int = 0
    processing_count: int = 0


class BulkPreviewPageData(BaseModel):
    batch: BulkBatchData
    items: list[BulkRecipientData]
    page: int
    size: int
    total: int


class BulkPreviewResponse(BaseModel):
    success: bool = True
    message: str | None = None
    data: BulkPreviewPageData


class BulkConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def clean_bulk_key(cls, value: str) -> str:
        cleaned = value.strip()
        if any(character.isspace() for character in cleaned):
            raise ValueError("idempotency_key cannot contain whitespace")
        return cleaned


class BulkCancelResponse(BaseModel):
    success: bool = True
    message: str
    data: dict[str, int]
