import re
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from pydantic import ValidationError

from app.core.config import settings
from app.core.rate_limit import lead_voice_rate_limiter
from app.providers.groq import GroqProvider
from app.schemas.lead import LeadFields
from app.schemas.lead_voice import (
    GroqLeadExtraction,
    LeadVoiceDraft,
    LeadVoiceExtractResponse,
    LeadVoiceFieldError,
)
from app.services.lead_voice_extraction_service import extract_lead_fields
from app.services.lead_voice_transcription_service import transcribe_lead_audio

SUPPORTED_AUDIO_CONTENT_TYPES = frozenset(
    {
        "audio/flac",
        "audio/m4a",
        "audio/mp3",
        "audio/mp4",
        "audio/mpeg",
        "audio/mpga",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "audio/x-m4a",
        "audio/x-wav",
        "video/mp4",
        "video/mpeg",
        "video/webm",
        "application/ogg",
    }
)
SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm"}
)
DEFAULT_AUDIO_EXTENSION = {
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mpga": ".mpga",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
    "video/mp4": ".mp4",
    "video/mpeg": ".mpeg",
    "video/webm": ".webm",
    "application/ogg": ".ogg",
}
PHONE_PATTERN = re.compile(r"^\+?[0-9০-৯][0-9০-৯ ()-]*$")
REVENUE_FIELDS = ("revenue_amount", "revenue_currency", "revenue_period")
PROVIDER_FIELDS = tuple(
    field
    for field in LeadVoiceDraft.model_fields
)


class LeadVoiceError(Exception):
    pass


class UnsupportedLeadAudioError(LeadVoiceError):
    pass


class LeadAudioTooLargeError(LeadVoiceError):
    pass


class LeadAudioTooLongError(LeadVoiceError):
    pass


class EmptyLeadAudioError(LeadVoiceError):
    pass


class SilentLeadAudioError(LeadVoiceError):
    pass


def _safe_filename(value: str | None, extension: str) -> str:
    name = Path(value or "voice-upload").name
    return name if Path(name).suffix else f"{name}{extension}"


async def _read_audio(upload: UploadFile) -> tuple[str, str, bytes]:
    content_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    extension = Path(upload.filename or "").suffix.lower()
    if content_type not in SUPPORTED_AUDIO_CONTENT_TYPES:
        await upload.close()
        raise UnsupportedLeadAudioError
    if extension and extension not in SUPPORTED_AUDIO_EXTENSIONS:
        await upload.close()
        raise UnsupportedLeadAudioError

    data = bytearray()
    try:
        while chunk := await upload.read(
            min(
                1024 * 1024,
                settings.lead_voice_max_upload_bytes - len(data) + 1,
            )
        ):
            data.extend(chunk)
            if len(data) > settings.lead_voice_max_upload_bytes:
                raise LeadAudioTooLargeError
    finally:
        # UploadFile uses a spooled temporary file; closing it removes any disk spill.
        await upload.close()
    if not data:
        raise EmptyLeadAudioError
    return (
        _safe_filename(
            upload.filename,
            extension or DEFAULT_AUDIO_EXTENSION[content_type],
        ),
        content_type,
        bytes(data),
    )


def _validation_message(exception: ValidationError) -> str:
    error = exception.errors()[0]
    return error["msg"].removeprefix("Value error, ")


def _validate_with_lead_fields(field: str, value: object) -> Any:
    payload: dict[str, object] = {
        "first_name": "Draft",
        "last_name": "Lead",
        "email": "draft@example.com",
        field: value,
    }
    if field == "revenue_amount":
        payload.update(revenue_currency="USD", revenue_period="annual")
    elif field == "revenue_currency":
        payload.update(revenue_amount="0", revenue_period="annual")
    elif field == "revenue_period":
        payload.update(revenue_amount="0", revenue_currency="USD")
    validated = LeadFields.model_validate(payload)
    result = getattr(validated, field)
    return str(result) if field == "email" and result is not None else result


def _name_is_ambiguous(value: str) -> bool:
    return any(character.isdigit() for character in value) or any(
        marker in value for marker in ("@", "://", "/")
    )


def _validate_phone(value: object) -> str:
    normalized = _validate_with_lead_fields("phone", value)
    if not isinstance(normalized, str) or not PHONE_PATTERN.fullmatch(normalized):
        raise ValueError("Phone contains invalid or ambiguous characters")
    digit_count = sum(character.isdigit() for character in normalized)
    if digit_count < 6 or digit_count > 20:
        raise ValueError("Phone must contain between 6 and 20 digits")
    return normalized


def _deduplicated_warnings(values: list[str]) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(str(value).strip().split())[:300]
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            warnings.append(cleaned)
        if len(warnings) >= 20:
            break
    return warnings


def validate_extracted_draft(
    extraction: GroqLeadExtraction,
) -> tuple[LeadVoiceDraft, list[LeadVoiceFieldError], list[str]]:
    raw = extraction.model_dump(mode="python")
    normalized: dict[str, Any] = {field: None for field in PROVIDER_FIELDS}
    errors: list[LeadVoiceFieldError] = []
    warnings = list(extraction.review_warnings)

    for field in PROVIDER_FIELDS:
        value = raw.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        try:
            if field == "phone":
                normalized[field] = _validate_phone(value)
            else:
                normalized[field] = _validate_with_lead_fields(field, value)
                if field in {"first_name", "last_name"} and _name_is_ambiguous(
                    str(normalized[field])
                ):
                    raise ValueError("Name is ambiguous and requires correction")
        except ValidationError as exception:
            errors.append(
                LeadVoiceFieldError(
                    field=field,
                    message=_validation_message(exception),
                )
            )
            normalized[field] = None
        except ValueError as exception:
            errors.append(LeadVoiceFieldError(field=field, message=str(exception)))
            normalized[field] = None

    supplied_revenue = [raw.get(field) not in (None, "") for field in REVENUE_FIELDS]
    if any(supplied_revenue) and not all(supplied_revenue):
        errors.append(
            LeadVoiceFieldError(
                field="revenue",
                message="Revenue amount, currency, and period must be provided together",
            )
        )

    has_any_name = bool(normalized["first_name"] or normalized["last_name"])
    has_complete_name = bool(normalized["first_name"] and normalized["last_name"])
    if extraction.name_split_uncertain or (has_any_name and not has_complete_name):
        warnings.append("Confirm first_name and last_name; the full-name split is uncertain.")

    return (
        LeadVoiceDraft.model_validate(normalized),
        errors,
        _deduplicated_warnings(warnings),
    )


async def extract_lead_voice_draft(
    upload: UploadFile,
    provider: GroqProvider,
    *,
    requester_key: str,
) -> LeadVoiceExtractResponse:
    await lead_voice_rate_limiter.enforce(
        requester_key,
        limit=settings.lead_voice_rate_limit_requests,
        window_seconds=settings.lead_voice_rate_limit_window_seconds,
    )
    filename, content_type, audio_bytes = await _read_audio(upload)
    transcription = await transcribe_lead_audio(
        provider,
        filename=filename,
        content_type=content_type,
        audio_bytes=audio_bytes,
        model=settings.groq_transcription_model,
    )
    if (
        transcription.duration_seconds is not None
        and transcription.duration_seconds > settings.lead_voice_max_duration_seconds
    ):
        raise LeadAudioTooLongError
    transcript = transcription.text.strip()
    if not transcript:
        raise SilentLeadAudioError

    extraction = await extract_lead_fields(
        provider,
        transcript=transcript,
        model=settings.groq_extraction_model,
    )
    draft, field_errors, review_warnings = validate_extracted_draft(extraction)
    return LeadVoiceExtractResponse(
        transcript=transcript,
        extracted_fields=draft,
        field_errors=field_errors,
        review_warnings=review_warnings,
    )
