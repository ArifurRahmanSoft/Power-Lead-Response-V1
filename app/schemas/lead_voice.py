from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.core.lead_catalogs import CompanySize, RevenuePeriod

GROQ_NULLABLE_LEAD_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "country",
    "designation",
    "company",
    "city",
    "state",
    "company_website",
    "industry",
    "company_size",
    "revenue_amount",
    "revenue_currency",
    "revenue_period",
    "linkedin_url",
    "facebook_url",
    "instagram_url",
    "x_url",
    "github_url",
    "service_requested",
    "notes",
)
GROQ_LEAD_EXTRACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        **{
            field: {"type": ["string", "null"]}
            for field in GROQ_NULLABLE_LEAD_FIELDS
        },
        "name_split_uncertain": {"type": "boolean"},
        "review_warnings": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        *GROQ_NULLABLE_LEAD_FIELDS,
        "name_split_uncertain",
        "review_warnings",
    ],
    "additionalProperties": False,
}


class GroqLeadExtraction(BaseModel):
    """Strict provider-facing shape. Every key is required but may be null."""

    model_config = ConfigDict(extra="forbid")

    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    country: str | None
    designation: str | None
    company: str | None
    city: str | None
    state: str | None
    company_website: str | None
    industry: str | None
    company_size: str | None
    revenue_amount: str | None
    revenue_currency: str | None
    revenue_period: str | None
    linkedin_url: str | None
    facebook_url: str | None
    instagram_url: str | None
    x_url: str | None
    github_url: str | None
    service_requested: str | None
    notes: str | None
    name_split_uncertain: bool
    review_warnings: list[str] = Field(max_length=20)


class LeadVoiceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    country: str | None = None
    designation: str | None = None
    company: str | None = None
    city: str | None = None
    state: str | None = None
    company_website: str | None = None
    industry: str | None = None
    company_size: CompanySize | None = None
    revenue_amount: Decimal | None = None
    revenue_currency: str | None = None
    revenue_period: RevenuePeriod | None = None
    linkedin_url: str | None = None
    facebook_url: str | None = None
    instagram_url: str | None = None
    x_url: str | None = None
    github_url: str | None = None
    service_requested: str | None = None
    notes: str | None = None


class LeadVoiceFieldError(BaseModel):
    field: str
    message: str


class LeadVoiceExtractResponse(BaseModel):
    transcript: str
    extracted_fields: LeadVoiceDraft
    field_errors: list[LeadVoiceFieldError]
    review_warnings: list[str]
