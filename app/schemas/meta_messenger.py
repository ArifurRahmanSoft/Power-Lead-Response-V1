import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MessengerChannelData(BaseModel):
    id: uuid.UUID
    provider: Literal["messenger"] = "messenger"
    page_id: str
    page_display_name: str
    display_name_available: bool
    profile_refreshed_at: datetime | None
    active: bool


class MessengerChannelResponse(BaseModel):
    success: bool = True
    message: str = "Messenger channel retrieved"
    data: MessengerChannelData


class MessengerBlockingReason(BaseModel):
    code: str
    message: str


class MessengerEligibilityData(BaseModel):
    reply_allowed: bool
    window_expires_at: datetime | None
    blocking_reasons: list[MessengerBlockingReason] = Field(default_factory=list)


class MessengerLinkedRecord(BaseModel):
    type: Literal["lead", "customer"]
    id: uuid.UUID
    display_name: str


class MessengerConversationData(BaseModel):
    id: uuid.UUID
    psid: str
    contact_display_name: str
    display_name_available: bool
    contact_display_name_source: Literal["linked_record", "provider_profile", "fallback"]
    page_display_name: str
    lead_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    linked_record: MessengerLinkedRecord | None
    last_message_at: datetime
    last_message_preview: str | None
    last_inbound_at: datetime | None
    unread_count: int
    eligibility: MessengerEligibilityData


class MessengerConversationPageData(BaseModel):
    items: list[MessengerConversationData]
    page: int
    size: int
    total: int
    pages: int


class MessengerConversationPageResponse(BaseModel):
    success: bool = True
    message: str = "Messenger conversations retrieved"
    data: MessengerConversationPageData


class MessengerConversationResponse(BaseModel):
    success: bool = True
    message: str = "Messenger conversation retrieved"
    data: MessengerConversationData


class MessengerMediaMetadata(BaseModel):
    type: str
    unsupported: bool = True


class MessengerMessageData(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    direction: Literal["inbound", "outbound"]
    provider: Literal["messenger"] = "messenger"
    source: Literal["contact", "crm", "page_inbox"]
    actor_type: Literal["contact", "crm_user", "page"]
    actor_user_id: uuid.UUID | None
    body: str
    media: list[MessengerMediaMetadata]
    status: str
    provider_message_id: str | None
    provider_error_code: str | None
    failure_reason: str | None
    requested_by_user_id: uuid.UUID | None
    occurred_at: datetime
    completed_at: datetime | None


class MessengerMessagePageData(BaseModel):
    conversation: MessengerConversationData
    items: list[MessengerMessageData]
    page: int
    size: int
    total: int
    pages: int


class MessengerMessagePageResponse(BaseModel):
    success: bool = True
    message: str = "Messenger messages retrieved"
    data: MessengerMessagePageData


class MessengerReplyRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    text: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(
        min_length=8, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$"
    )

    @model_validator(mode="after")
    def validate_text(self) -> "MessengerReplyRequest":
        if not self.text.strip():
            raise ValueError("text must not be blank")
        if "\x00" in self.text:
            raise ValueError("text contains unsupported characters")
        return self


class MessengerReplyData(BaseModel):
    message: MessengerMessageData
    replayed: bool
    operation_state: Literal["in_progress", "completed"]


class MessengerReplyResponse(BaseModel):
    success: bool = True
    message: str = "Messenger reply processed"
    data: MessengerReplyData


class MessengerRecoveryData(BaseModel):
    idempotency_key: str
    found: bool
    operation_state: Literal["missing", "in_progress", "completed"]
    message: MessengerMessageData | None


class MessengerRecoveryResponse(BaseModel):
    success: bool = True
    message: str = "Messenger reply operation retrieved"
    data: MessengerRecoveryData


class MessengerReadData(BaseModel):
    conversation_id: uuid.UUID
    unread_count: int = 0
    last_read_at: datetime


class MessengerReadResponse(BaseModel):
    success: bool = True
    message: str = "Messenger conversation marked read"
    data: MessengerReadData


class MessengerConversationLinkRequest(BaseModel):
    lead_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "MessengerConversationLinkRequest":
        if (self.lead_id is None) == (self.customer_id is None):
            raise ValueError("Provide exactly one of lead_id or customer_id")
        return self


class MessengerProfileRefreshRequest(BaseModel):
    conversation_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    refresh_page: bool = True


class MessengerProfileRefreshItem(BaseModel):
    conversation_id: uuid.UUID
    status: Literal["refreshed", "cached", "unavailable"]
    contact_display_name: str
    display_name_available: bool
    reason_code: str | None = None


class MessengerProfileRefreshData(BaseModel):
    page_display_name: str
    page_status: Literal["refreshed", "cached", "unavailable", "skipped"]
    items: list[MessengerProfileRefreshItem]
    requested_count: int
    refreshed_count: int
    unavailable_count: int


class MessengerProfileRefreshResponse(BaseModel):
    success: bool = True
    message: str = "Messenger display-name refresh processed"
    data: MessengerProfileRefreshData
