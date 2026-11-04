import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.core.lead_catalogs import (
    CURRENCY_CODES,
    CompanySize,
    ConsentStatus,
    RevenuePeriod,
)
from app.utils.lead_normalization import (
    normalize_country_name,
    normalize_optional_phone_number,
    normalize_optional_url,
    normalize_optional_whatsapp_number,
)


class CustomerDuplicateResolution(StrEnum):
    CREATE_NEW = "create_new"
    LINK_EXISTING = "link_existing"


class AcquisitionTaskDisposition(StrEnum):
    CANCEL = "cancel"
    RETAIN = "retain"


def _clean_optional(value: object) -> object:
    if isinstance(value, str):
        return " ".join(value.strip().split()) or None
    return value


CUSTOMER_URL_FIELDS = (
    "linkedin_url",
    "facebook_url",
    "instagram_url",
    "x_url",
    "github_url",
    "company_website",
)
CUSTOMER_TEXT_FIELDS = (
    "designation",
    "company",
    "phone",
    "whatsapp_number",
    "city",
    "state",
    "industry",
    "service_requested",
    "notes",
    "consent_evidence",
    "consent_reference",
)
CUSTOMER_WRITABLE_FIELDS = (
    "first_name",
    "last_name",
    "email",
    *CUSTOMER_TEXT_FIELDS,
    "country",
    *CUSTOMER_URL_FIELDS,
    "company_size",
    "revenue_amount",
    "revenue_currency",
    "revenue_period",
    "assigned_user_id",
    "communication_consent_status",
)


class CustomerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    designation: str | None = Field(default=None, max_length=160)
    company: str | None = Field(default=None, max_length=200)
    country: str | None = None
    linkedin_url: str | None = Field(default=None, max_length=2048)
    facebook_url: str | None = Field(default=None, max_length=2048)
    instagram_url: str | None = Field(default=None, max_length=2048)
    x_url: str | None = Field(default=None, max_length=2048)
    github_url: str | None = Field(default=None, max_length=2048)
    phone: str | None = Field(default=None, max_length=80)
    whatsapp_number: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    company_website: str | None = Field(default=None, max_length=2048)
    industry: str | None = Field(default=None, max_length=160)
    service_requested: str | None = Field(default=None, max_length=200)
    company_size: CompanySize | None = None
    revenue_amount: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=18,
        decimal_places=2,
    )
    revenue_currency: str | None = None
    revenue_period: RevenuePeriod | None = None
    notes: str | None = Field(default=None, max_length=10000)
    assigned_user_id: uuid.UUID | None = None
    communication_consent_status: ConsentStatus = ConsentStatus.UNKNOWN
    consent_evidence: str | None = Field(default=None, max_length=4000)
    consent_reference: str | None = Field(default=None, max_length=255)

    @field_validator("first_name", "last_name")
    @classmethod
    def clean_required_name(cls, value: str) -> str:
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError("Field is required")
        return cleaned

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator(*CUSTOMER_TEXT_FIELDS, mode="before")
    @classmethod
    def clean_optional_text(cls, value: object) -> object:
        return _clean_optional(value)

    @field_validator(*CUSTOMER_URL_FIELDS, mode="before")
    @classmethod
    def validate_url(cls, value: object) -> object:
        return normalize_optional_url(value)

    @field_validator("country", mode="before")
    @classmethod
    def validate_country(cls, value: object) -> object:
        return normalize_country_name(value)

    @field_validator("phone")
    @classmethod
    def normalize_phone(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        return normalize_optional_phone_number(value, country=info.data.get("country"))

    @field_validator("whatsapp_number")
    @classmethod
    def normalize_whatsapp(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        return normalize_optional_whatsapp_number(
            value,
            country=info.data.get("country"),
        )

    @field_validator("revenue_currency", mode="before")
    @classmethod
    def validate_currency(cls, value: object) -> object:
        if value in (None, ""):
            return None
        code = str(value).strip().upper()
        if code not in CURRENCY_CODES:
            raise ValueError("Revenue currency must be a valid ISO 4217 code")
        return code

    @model_validator(mode="after")
    def validate_revenue(self) -> Self:
        values = (self.revenue_amount, self.revenue_currency, self.revenue_period)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError(
                "Revenue amount, currency, and period must be provided together"
            )
        return self


class CustomerUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    first_name: str | None = Field(default=None, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = None
    designation: str | None = Field(default=None, max_length=160)
    company: str | None = Field(default=None, max_length=200)
    country: str | None = None
    linkedin_url: str | None = Field(default=None, max_length=2048)
    facebook_url: str | None = Field(default=None, max_length=2048)
    instagram_url: str | None = Field(default=None, max_length=2048)
    x_url: str | None = Field(default=None, max_length=2048)
    github_url: str | None = Field(default=None, max_length=2048)
    phone: str | None = Field(default=None, max_length=80)
    whatsapp_number: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    company_website: str | None = Field(default=None, max_length=2048)
    industry: str | None = Field(default=None, max_length=160)
    service_requested: str | None = Field(default=None, max_length=200)
    company_size: CompanySize | None = None
    revenue_amount: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=18,
        decimal_places=2,
    )
    revenue_currency: str | None = None
    revenue_period: RevenuePeriod | None = None
    notes: str | None = Field(default=None, max_length=10000)
    assigned_user_id: uuid.UUID | None = None
    communication_consent_status: ConsentStatus | None = None
    consent_evidence: str | None = Field(default=None, max_length=4000)
    consent_reference: str | None = Field(default=None, max_length=255)

    @field_validator("first_name", "last_name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            raise ValueError("Field cannot be blank")
        return cleaned

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator(*CUSTOMER_TEXT_FIELDS, mode="before")
    @classmethod
    def clean_optional_text(cls, value: object) -> object:
        return _clean_optional(value)

    @field_validator(*CUSTOMER_URL_FIELDS, mode="before")
    @classmethod
    def validate_url(cls, value: object) -> object:
        return normalize_optional_url(value)

    @field_validator("country", mode="before")
    @classmethod
    def validate_country(cls, value: object) -> object:
        return normalize_country_name(value)

    @field_validator("revenue_currency", mode="before")
    @classmethod
    def validate_currency(cls, value: object) -> object:
        if value in (None, ""):
            return None
        code = str(value).strip().upper()
        if code not in CURRENCY_CODES:
            raise ValueError("Revenue currency must be a valid ISO 4217 code")
        return code

    @model_validator(mode="after")
    def validate_update(self) -> Self:
        changed = self.model_fields_set - {"row_version"}
        if not changed:
            raise ValueError("Provide at least one field to update")
        for field in (
            "first_name",
            "last_name",
            "email",
            "communication_consent_status",
        ):
            if field in changed and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class LeadConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    purchased_service: str | None = Field(default=None, max_length=200)
    reference: str | None = Field(default=None, max_length=255)
    reason: str | None = Field(default=None, max_length=1000)
    duplicate_resolution: CustomerDuplicateResolution = (
        CustomerDuplicateResolution.CREATE_NEW
    )
    existing_customer_id: uuid.UUID | None = None
    pending_acquisition_task_disposition: AcquisitionTaskDisposition = (
        AcquisitionTaskDisposition.CANCEL
    )
    pending_task_reason: str | None = Field(default=None, max_length=1000)

    _normalize_optional = field_validator(
        "purchased_service",
        "reference",
        "reason",
        "pending_task_reason",
        mode="before",
    )(_clean_optional)

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        if (
            self.duplicate_resolution == CustomerDuplicateResolution.LINK_EXISTING
            and self.existing_customer_id is None
        ):
            raise ValueError(
                "existing_customer_id is required when duplicate_resolution is link_existing"
            )
        if (
            self.duplicate_resolution == CustomerDuplicateResolution.CREATE_NEW
            and self.existing_customer_id is not None
        ):
            raise ValueError(
                "existing_customer_id is only allowed when duplicate_resolution is link_existing"
            )
        return self


class CustomerData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str
    last_name: str
    email: EmailStr
    designation: str | None
    country: str | None
    linkedin_url: str | None
    facebook_url: str | None
    instagram_url: str | None
    x_url: str | None
    github_url: str | None
    phone: str | None
    whatsapp_number: str | None = None
    company: str | None
    city: str | None
    state: str | None
    company_website: str | None
    industry: str | None
    service_requested: str | None
    company_size: CompanySize | None
    revenue_amount: Decimal | None
    revenue_currency: str | None
    revenue_period: RevenuePeriod | None
    notes: str | None
    assigned_user_id: uuid.UUID | None
    source_type: Literal["direct", "lead"] | None = None
    source_lead_id: uuid.UUID | None = None
    source_lead_label: str | None = None
    converted_at: datetime | None = None
    communication_purpose: str
    communication_consent_status: ConsentStatus
    consent_evidence: str | None
    consent_reference: str | None
    channel_restrictions: list[str]
    suppressed_at: datetime | None
    suppression_reason: str | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    row_version: int

    @field_validator("country", mode="before")
    @classmethod
    def expose_country_name(cls, value: object) -> object:
        return normalize_country_name(value)

    @field_validator(
        "suppressed_at",
        "created_at",
        "updated_at",
        "converted_at",
        mode="before",
    )
    @classmethod
    def expose_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class LeadConversionData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    customer_id: uuid.UUID
    converted_at: datetime
    converted_by: uuid.UUID
    purchased_service: str | None
    reference: str | None
    reason: str | None
    duplicate_resolution: CustomerDuplicateResolution
    pending_task_disposition: AcquisitionTaskDisposition
    pending_task_reason: str
    cancelled_nurture_job_count: int
    affected_task_count: int
    source_consent_status: str
    source_consent_evidence: str | None
    source_consent_reference: str | None

    @field_validator("converted_at", mode="before")
    @classmethod
    def expose_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class LeadConversionResponseData(BaseModel):
    customer: CustomerData
    conversion: LeadConversionData
    idempotent_replay: bool


class LeadConversionResponse(BaseModel):
    success: bool
    message: str
    data: LeadConversionResponseData


class CustomerListData(BaseModel):
    items: list[CustomerData]
    page: int
    size: int
    total: int


class CustomerListResponse(BaseModel):
    success: bool
    data: CustomerListData


class CustomerResponse(BaseModel):
    success: bool
    message: str
    data: CustomerData


class CustomerDetailData(CustomerData):
    conversions: list[LeadConversionData]


class CustomerDetailResponse(BaseModel):
    success: bool
    data: CustomerDetailData
