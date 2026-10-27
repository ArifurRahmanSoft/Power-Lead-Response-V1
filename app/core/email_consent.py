from typing import Any


EXPLICIT_EMAIL_CONSENT_DENIALS = frozenset({"denied", "withdrawn"})


def normalize_email_consent_status(value: Any) -> str:
    """Normalize for an eligibility decision without changing stored data."""
    if value is None:
        return ""
    normalized = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    return "_".join(part for part in normalized.split("_") if part)


def is_email_consent_denied(value: Any) -> bool:
    """Only explicit refusal or a later withdrawal blocks email sending."""
    return normalize_email_consent_status(value) in EXPLICIT_EMAIL_CONSENT_DENIALS


def email_consent_denial_message(subject: str, value: Any) -> str | None:
    if not is_email_consent_denied(value):
        return None
    normalized = normalize_email_consent_status(value)
    if normalized == "denied":
        return f"{subject} email consent is explicitly denied"
    if normalized == "withdrawn":
        return f"{subject} email consent was withdrawn"
    return f"{subject} email consent is explicitly denied"
