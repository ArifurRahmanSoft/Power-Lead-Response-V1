import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.core.lead_catalogs import (
    CURRENCY_CODES,
    CompanySize,
    ConsentStatus,
    EmailStatus,
    LeadSource,
    LeadStatus,
    RevenuePeriod,
)
from app.utils.lead_normalization import normalize_country_name, normalize_optional_url

URL_FIELDS = (
    "linkedin_url",
    "facebook_url",
    "instagram_url",
    "x_url",
    "github_url",
    "company_website",
)
TEXT_FIELDS = (
    "designation",
    "company",
    "phone",
    "city",
    "state",
    "industry",
    "service_requested",
    "notes",
    "consent_evidence",
    "consent_reference",
)


class LeadFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    designation: str | None = Field(default=None, max_length=160)
    company: str | None = Field(default=None, max_length=200)
    country: str | None = None
    email_status: EmailStatus = EmailStatus.UNKNOWN
    linkedin_url: str | None = Field(default=None, max_length=2048)
    facebook_url: str | None = Field(default=None, max_length=2048)
    instagram_url: str | None = Field(default=None, max_length=2048)
    x_url: str | None = Field(default=None, max_length=2048)
    github_url: str | None = Field(default=None, max_length=2048)
    phone: str | None = Field(default=None, max_length=80)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    company_website: str | None = Field(default=None, max_length=2048)
    industry: str | None = Field(default=None, max_length=160)
    service_requested: str | None = Field(default=None, max_length=200)
    company_size: CompanySize | None = None
    revenue_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    revenue_currency: str | None = None
    revenue_period: RevenuePeriod | None = None
    notes: str | None = Field(default=None, max_length=10000)
    consent_status: ConsentStatus = ConsentStatus.UNKNOWN
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

    @field_validator(*TEXT_FIELDS, mode="before")
    @classmethod
    def clean_optional_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        return cleaned or None

    @field_validator(*URL_FIELDS, mode="before")
    @classmethod
    def validate_http_url(cls, value: object) -> object:
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
    def validate_revenue_and_consent(self) -> Self:
        revenue_values = (self.revenue_amount, self.revenue_currency, self.revenue_period)
        if any(value is not None for value in revenue_values) and not all(
            value is not None for value in revenue_values
        ):
            raise ValueError(
                "Revenue amount, currency, and period must be provided together"
            )
        if self.consent_status == ConsentStatus.GRANTED and not (
            self.consent_evidence or self.consent_reference
        ):
            raise ValueError("Granted consent requires evidence or a reference")
        return self


class LeadCreateRequest(LeadFields):
    status: LeadStatus = LeadStatus.NEW
    assigned_user_id: uuid.UUID | None = None


class LeadUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    first_name: str | None = Field(default=None, min_length=1, max_length=120)
    last_name: str | None = Field(default=None, min_length=1, max_length=120)
    email: EmailStr | None = None
    designation: str | None = Field(default=None, max_length=160)
    company: str | None = Field(default=None, max_length=200)
    country: str | None = None
    email_status: EmailStatus | None = None
    linkedin_url: str | None = Field(default=None, max_length=2048)
    facebook_url: str | None = Field(default=None, max_length=2048)
    instagram_url: str | None = Field(default=None, max_length=2048)
    x_url: str | None = Field(default=None, max_length=2048)
    github_url: str | None = Field(default=None, max_length=2048)
    phone: str | None = Field(default=None, max_length=80)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    company_website: str | None = Field(default=None, max_length=2048)
    industry: str | None = Field(default=None, max_length=160)
    service_requested: str | None = Field(default=None, max_length=200)
    company_size: CompanySize | None = None
    revenue_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    revenue_currency: str | None = None
    revenue_period: RevenuePeriod | None = None
    notes: str | None = Field(default=None, max_length=10000)
    status: LeadStatus | None = None
    assigned_user_id: uuid.UUID | None = None
    consent_status: ConsentStatus | None = None
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

    @field_validator(*TEXT_FIELDS, mode="before")
    @classmethod
    def clean_optional_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @field_validator(*URL_FIELDS, mode="before")
    @classmethod
    def validate_http_url(cls, value: object) -> object:
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
    def require_change(self) -> Self:
        if not (self.model_fields_set - {"row_version"}):
            raise ValueError("Provide at least one field to update")
        return self


class LeadData(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tracking_id: str
    first_name: str
    last_name: str
    email: EmailStr
    designation: str | None
    company: str | None
    country: str | None
    email_status: EmailStatus
    linkedin_url: str | None
    facebook_url: str | None
    instagram_url: str | None
    x_url: str | None
    github_url: str | None
    phone: str | None
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
    source: LeadSource
    status: LeadStatus
    assigned_user_id: uuid.UUID | None
    consent_status: ConsentStatus
    consent_evidence: str | None
    consent_reference: str | None
    import_batch_id: uuid.UUID | None
    created_at: datetime
    created_by: uuid.UUID
    updated_at: datetime
    updated_by: uuid.UUID
    row_version: int

    @field_validator("country", mode="before")
    @classmethod
    def expose_country_name(cls, value: object) -> object:
        return normalize_country_name(value)


class LeadResponse(BaseModel):
    success: bool
    message: str | None = None
    data: LeadData


class LeadListData(BaseModel):
    items: list[LeadData]
    page: int
    size: int
    total: int


class LeadListResponse(BaseModel):
    success: bool
    data: LeadListData


class LeadServiceOption(BaseModel):
    label: str
    value: str


class LeadServiceOptionsData(BaseModel):
    items: list[LeadServiceOption]
    page: int
    size: int
    total: int


class LeadServiceOptionsResponse(BaseModel):
    success: bool
    data: LeadServiceOptionsData


class LeadDeleteResponse(BaseModel):
    success: bool
    message: str


class LeadImportRowError(BaseModel):
    field: str
    message: str


class LeadImportRowData(BaseModel):
    row_number: int
    status: str
    errors: list[LeadImportRowError]
    lead_id: uuid.UUID | None = None


class LeadImportBatchData(BaseModel):
    id: uuid.UUID
    status: str
    file_name: str
    mapping: dict[str, str]
    summary: dict[str, int]
    expires_at: datetime
    created_at: datetime
    committed_at: datetime | None


class LeadImportBatchResponse(BaseModel):
    success: bool
    message: str | None = None
    data: LeadImportBatchData


class LeadImportErrorsData(BaseModel):
    items: list[LeadImportRowData]
    page: int
    size: int
    total: int


class LeadImportErrorsResponse(BaseModel):
    success: bool
    data: LeadImportErrorsData


class LeadFilterParams(BaseModel):
    search: str | None = None
    name: str | None = None
    email: str | None = None
    company: str | None = None
    country: str | None = None
    industry: str | None = None
    service_requested: str | None = Field(default=None, max_length=200)
    status: LeadStatus | None = None
    email_status: EmailStatus | None = None
    source: LeadSource | None = None
    assigned_user_id: uuid.UUID | None = None

    @field_validator("country", mode="before")
    @classmethod
    def normalize_country_filter(cls, value: object) -> object:
        return normalize_country_name(value)

    @field_validator("service_requested", mode="before")
    @classmethod
    def normalize_service_filter(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None


def validation_errors(exception: Any) -> list[dict[str, str]]:
    return [
        {
            "field": ".".join(str(part) for part in error["loc"]) or "row",
            "message": error["msg"].removeprefix("Value error, "),
        }
        for error in exception.errors()
    ]
