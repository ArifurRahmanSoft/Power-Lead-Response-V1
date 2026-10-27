import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MetaChannelData(BaseModel):
    id: uuid.UUID
    provider: Literal["meta"] = "meta"
    waba_id: str
    phone_number_id: str
    sender_address: str
    active: bool


class MetaChannelResponse(BaseModel):
    success: bool = True
    message: str = "Meta WhatsApp channel retrieved"
    data: MetaChannelData


class MetaEligibilityData(BaseModel):
    free_form_allowed: bool
    service_window_expires_at: datetime | None
    approved_template_required: bool
    template_outreach_allowed: bool
    restrictions: list[dict[str, str]] = Field(default_factory=list)


class MetaConversationData(BaseModel):
    id: uuid.UUID
    external_address: str
    lead_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    contact_name: str | None
    last_message_at: datetime
    last_message_preview: str | None
    unread_count: int
    eligibility: MetaEligibilityData


class MetaConversationPageData(BaseModel):
    items: list[MetaConversationData]
    page: int
    size: int
    total: int
    pages: int


class MetaConversationPageResponse(BaseModel):
    success: bool = True
    message: str = "Meta WhatsApp conversations retrieved"
    data: MetaConversationPageData


class MetaOpenConversationRequest(BaseModel):
    phone_number: str = Field(min_length=8, max_length=32)


class MetaConversationResponse(BaseModel):
    success: bool = True
    message: str = "Meta WhatsApp conversation retrieved"
    data: MetaConversationData


class MetaMediaMetadata(BaseModel):
    type: str
    provider_media_id: str | None = None
    unsupported: bool = True


class MetaMessageData(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    direction: str
    provider: Literal["meta"] = "meta"
    mode: str
    purpose: str | None
    body: str
    media: list[MetaMediaMetadata]
    status: str
    provider_message_id: str | None
    provider_error_code: str | None
    failure_reason: str | None
    requested_by_user_id: uuid.UUID | None
    occurred_at: datetime
    completed_at: datetime | None


class MetaMessagePageData(BaseModel):
    conversation: MetaConversationData
    items: list[MetaMessageData]
    page: int
    size: int
    total: int
    pages: int


class MetaMessagePageResponse(BaseModel):
    success: bool = True
    message: str = "Meta WhatsApp messages retrieved"
    data: MetaMessagePageData


class MetaTemplateParameter(BaseModel):
    component: Literal["header", "body", "button"]
    index: int = Field(ge=1, le=20)
    value: str = Field(min_length=1, max_length=1024)


class MetaApprovedTemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    language: str = Field(min_length=2, max_length=20)
    parameters: list[MetaTemplateParameter] = Field(default_factory=list, max_length=60)


class MetaSendRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    conversation_id: uuid.UUID | None = None
    phone_number: str | None = Field(default=None, min_length=8, max_length=32)
    mode: Literal["text", "approved_template"]
    text: str | None = Field(default=None, max_length=4096)
    template: MetaApprovedTemplateRequest | None = None
    purpose: Literal["lead_acquisition", "customer_service"]
    idempotency_key: str = Field(
        min_length=8, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$"
    )

    @model_validator(mode="after")
    def validate_mode(self) -> "MetaSendRequest":
        if self.conversation_id is None and self.phone_number is None:
            raise ValueError("conversation_id or phone_number is required")
        if self.mode == "text":
            if self.template is not None:
                raise ValueError("template is not allowed for text mode")
            if self.text is None or not self.text.strip():
                raise ValueError("text is required for text mode")
            if "\x00" in self.text:
                raise ValueError("text contains unsupported characters")
        else:
            if self.text is not None:
                raise ValueError("text is not allowed for approved_template mode")
            if self.template is None:
                raise ValueError("template is required for approved_template mode")
        return self


class MetaSendData(BaseModel):
    message: MetaMessageData
    replayed: bool
    operation_state: Literal["in_progress", "completed"]


class MetaSendResponse(BaseModel):
    success: bool = True
    message: str = "Meta WhatsApp send processed"
    data: MetaSendData


class MetaRecoveryData(BaseModel):
    idempotency_key: str
    found: bool
    operation_state: Literal["missing", "in_progress", "completed"]
    message: MetaMessageData | None


class MetaRecoveryResponse(BaseModel):
    success: bool = True
    message: str = "Meta WhatsApp operation state retrieved"
    data: MetaRecoveryData


class MetaTemplateVariable(BaseModel):
    component: str
    index: int


class MetaTemplateData(BaseModel):
    id: str | None
    name: str
    language: str
    category: str | None
    provider_status: str
    usable: bool
    components: list[dict[str, object]]
    required_variables: list[MetaTemplateVariable]


class MetaTemplatePageData(BaseModel):
    items: list[MetaTemplateData]
    page: int
    size: int
    total: int
    pages: int


class MetaTemplatePageResponse(BaseModel):
    success: bool = True
    message: str = "Meta WhatsApp templates retrieved"
    data: MetaTemplatePageData


class MetaReadData(BaseModel):
    conversation_id: uuid.UUID
    unread_count: int = 0
    last_read_at: datetime


class MetaReadResponse(BaseModel):
    success: bool = True
    message: str = "Meta WhatsApp conversation marked read"
    data: MetaReadData


class MetaConversationLinkRequest(BaseModel):
    lead_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "MetaConversationLinkRequest":
        if (self.lead_id is None) == (self.customer_id is None):
            raise ValueError("Provide exactly one of lead_id or customer_id")
        return self
