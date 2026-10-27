import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WhatsAppConversationData(BaseModel):
    id: uuid.UUID
    external_address: str
    lead_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    contact_name: str | None
    last_message_at: datetime
    last_message_preview: str | None
    unread_count: int
    free_form_reply_allowed: bool
    service_window_expires_at: datetime | None


class WhatsAppConversationPageData(BaseModel):
    items: list[WhatsAppConversationData]
    page: int
    size: int
    total: int
    pages: int


class WhatsAppConversationPageResponse(BaseModel):
    success: bool = True
    message: str = "WhatsApp conversations retrieved"
    data: WhatsAppConversationPageData


class WhatsAppMediaMetadata(BaseModel):
    index: int
    content_type: str | None = None
    unsupported: bool = True


class WhatsAppMessageData(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    direction: str
    body: str
    media: list[WhatsAppMediaMetadata]
    status: str
    provider_message_sid: str | None
    provider_error_code: str | None
    failure_reason: str | None
    requested_by_user_id: uuid.UUID | None
    occurred_at: datetime
    completed_at: datetime | None


class WhatsAppMessagePageData(BaseModel):
    conversation: WhatsAppConversationData
    items: list[WhatsAppMessageData]
    page: int
    size: int
    total: int
    pages: int


class WhatsAppMessagePageResponse(BaseModel):
    success: bool = True
    message: str = "WhatsApp messages retrieved"
    data: WhatsAppMessagePageData


class WhatsAppReplyRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    body: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=8, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")

    @model_validator(mode="after")
    def validate_body(self) -> "WhatsAppReplyRequest":
        if not self.body.strip():
            raise ValueError("body is required")
        if "\x00" in self.body:
            raise ValueError("body contains unsupported characters")
        return self


class WhatsAppReplyData(BaseModel):
    message: WhatsAppMessageData
    replayed: bool
    operation_state: str


class WhatsAppReplyResponse(BaseModel):
    success: bool = True
    message: str = "WhatsApp reply processed"
    data: WhatsAppReplyData


class WhatsAppRecoveryData(BaseModel):
    idempotency_key: str
    found: bool
    operation_state: str
    message: WhatsAppMessageData | None


class WhatsAppRecoveryResponse(BaseModel):
    success: bool = True
    message: str = "WhatsApp operation state retrieved"
    data: WhatsAppRecoveryData


class WhatsAppReadData(BaseModel):
    conversation_id: uuid.UUID
    unread_count: int = 0
    last_read_at: datetime


class WhatsAppReadResponse(BaseModel):
    success: bool = True
    message: str = "Conversation marked read"
    data: WhatsAppReadData


class WhatsAppConversationLinkRequest(BaseModel):
    lead_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "WhatsAppConversationLinkRequest":
        if (self.lead_id is None) == (self.customer_id is None):
            raise ValueError("Provide exactly one of lead_id or customer_id")
        return self


class WhatsAppConversationResponse(BaseModel):
    success: bool = True
    message: str = "WhatsApp conversation retrieved"
    data: WhatsAppConversationData
