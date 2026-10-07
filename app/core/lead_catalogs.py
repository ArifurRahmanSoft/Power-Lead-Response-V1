from enum import StrEnum

import pycountry


class EmailStatus(StrEnum):
    UNKNOWN = "unknown"
    UNVERIFIED = "unverified"
    VALID = "valid"
    INVALID = "invalid"
    BOUNCED = "bounced"
    RISKY = "risky"
    DISPOSABLE = "disposable"


class CompanySize(StrEnum):
    SELF_EMPLOYED = "self_employed"
    SIZE_1_10 = "1_10"
    SIZE_11_50 = "11_50"
    SIZE_51_200 = "51_200"
    SIZE_201_500 = "201_500"
    SIZE_501_1000 = "501_1000"
    SIZE_1001_5000 = "1001_5000"
    SIZE_5001_10000 = "5001_10000"
    SIZE_10000_PLUS = "10000_plus"


class RevenuePeriod(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class LeadStatus(StrEnum):
    NEW = "new"
    QUALIFIED = "qualified"
    CONTACTED = "contacted"
    ENGAGED = "engaged"
    CONVERTED = "converted"
    DISQUALIFIED = "disqualified"


class LeadSource(StrEnum):
    MANUAL = "manual"
    IMPORT = "import"


class ConsentStatus(StrEnum):
    UNKNOWN = "unknown"
    NOT_REQUESTED = "not_requested"
    GRANTED = "granted"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"


LEAD_STATUS_TRANSITIONS: dict[LeadStatus, frozenset[LeadStatus]] = {
    LeadStatus.NEW: frozenset({LeadStatus.QUALIFIED, LeadStatus.CONTACTED, LeadStatus.DISQUALIFIED}),
    LeadStatus.QUALIFIED: frozenset({LeadStatus.CONTACTED, LeadStatus.DISQUALIFIED}),
    LeadStatus.CONTACTED: frozenset({LeadStatus.ENGAGED, LeadStatus.DISQUALIFIED}),
    LeadStatus.ENGAGED: frozenset({LeadStatus.CONVERTED, LeadStatus.DISQUALIFIED}),
    LeadStatus.CONVERTED: frozenset(),
    LeadStatus.DISQUALIFIED: frozenset({LeadStatus.NEW}),
}

COUNTRY_CODES = frozenset(country.alpha_2 for country in pycountry.countries)
CURRENCY_CODES = frozenset(currency.alpha_3 for currency in pycountry.currencies)

