import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InstagramChannelData(BaseModel):
    id: uuid.UUID
    provider: Literal["instagram"] = "instagram"
    account_id: str
    account_display_name: str
    configuration_id: str | None
    ready: bool
    active: bool


class InstagramChannelResponse(BaseModel):
    success: bool = True
    message: str = "Instagram channel retrieved"
    data: InstagramChannelData


class InstagramBlockingReason(BaseModel):
    code: str
    message: str


class InstagramEligibilityData(BaseModel):
    reply_allowed: bool
    window_expires_at: datetime | None
    blocking_reasons: list[InstagramBlockingReason]


class InstagramLinkedRecord(BaseModel):
    type: Literal["lead", "customer"]
    id: uuid.UUID
    display_name: str


class InstagramConversationData(BaseModel):
    id: uuid.UUID
    igsid: str
    contact_display_name: str
    display_name_available: bool
    contact_display_name_source: Literal["linked_record", "provider_profile", "fallback"]
    account_display_name: str
    lead_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    linked_record: InstagramLinkedRecord | None
    last_message_at: datetime
    last_message_preview: str | None
    last_inbound_at: datetime | None
    unread_count: int
    eligibility: InstagramEligibilityData


class InstagramConversationPageData(BaseModel):
    items: list[InstagramConversationData]
    page: int
    size: int
    total: int
    pages: int


class InstagramConversationPageResponse(BaseModel):
    success: bool = True
    message: str = "Instagram conversations retrieved"
    data: InstagramConversationPageData


class InstagramConversationResponse(BaseModel):
    success: bool = True
    message: str
    data: InstagramConversationData


class InstagramMediaMetadata(BaseModel):
    type: Literal["audio", "file", "image", "video", "media", "ig_post", "story", "attachment"]
    unsupported: bool = True


class InstagramMessageData(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    direction: Literal["inbound", "outbound"]
    provider: Literal["instagram"] = "instagram"
    source: Literal["contact", "crm", "instagram_inbox"]
    actor_type: Literal["contact", "crm_user", "instagram_account"]
    actor_user_id: uuid.UUID | None
    body: str
    media: list[InstagramMediaMetadata]
    status: Literal["received", "accepted", "read", "failed", "unknown"]
    provider_message_id: str | None
    provider_error_code: str | None
    failure_reason: str | None
    requested_by_user_id: uuid.UUID | None
    occurred_at: datetime
    completed_at: datetime | None


class InstagramMessagePageData(BaseModel):
    conversation: InstagramConversationData
    items: list[InstagramMessageData]
    page: int
    size: int
    total: int
    pages: int


class InstagramMessagePageResponse(BaseModel):
    success: bool = True
    message: str = "Instagram messages retrieved"
    data: InstagramMessagePageData


class InstagramReplyRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    text: str = Field(min_length=1, max_length=1000)
    idempotency_key: str = Field(
        min_length=8,
        max_length=120,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )

    @model_validator(mode="after")
    def validate_text(self) -> "InstagramReplyRequest":
        if not self.text.strip():
            raise ValueError("text must not be blank")
        if "\x00" in self.text:
            raise ValueError("text contains unsupported characters")
        if len(self.text.encode("utf-8")) > 1000:
            raise ValueError("text must be at most 1000 UTF-8 bytes")
        return self


class InstagramReplyData(BaseModel):
    message: InstagramMessageData
    replayed: bool
    operation_state: Literal["in_progress", "completed"]


class InstagramReplyResponse(BaseModel):
    success: bool = True
    message: str = "Instagram reply processed"
    data: InstagramReplyData


class InstagramRecoveryData(BaseModel):
    idempotency_key: str
    found: bool
    operation_state: Literal["missing", "in_progress", "completed"]
    message: InstagramMessageData | None


class InstagramRecoveryResponse(BaseModel):
    success: bool = True
    message: str = "Instagram reply operation retrieved"
    data: InstagramRecoveryData


class InstagramReadData(BaseModel):
    conversation_id: uuid.UUID
    unread_count: int = 0
    last_read_at: datetime
    provider_receipt_sent: Literal[False] = False


class InstagramReadResponse(BaseModel):
    success: bool = True
    message: str = "Instagram conversation marked read in CRM"
    data: InstagramReadData


class InstagramConversationLinkRequest(BaseModel):
    lead_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "InstagramConversationLinkRequest":
        if (self.lead_id is None) == (self.customer_id is None):
            raise ValueError("Provide exactly one of lead_id or customer_id")
        return self
