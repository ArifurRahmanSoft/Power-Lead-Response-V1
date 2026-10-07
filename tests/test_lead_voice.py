from copy import deepcopy
from typing import Any
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.jwt import create_access_token
from app.core.permissions import PermissionKey
from app.main import app
from app.models.lead import Lead
from app.models.membership import TenantMembership
from app.models.role import Permission, Role, RolePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.providers.groq import (
    GroqMalformedResponseError,
    GroqProvider,
    GroqProviderError,
    GroqRateLimitError,
    GroqTimeoutError,
    GroqTranscription,
    get_groq_provider,
)
from app.schemas.lead_voice import GroqLeadExtraction
from app.utils.password import hash_password


def voice_extraction(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        field: None for field in GroqLeadExtraction.model_fields
    }
    payload.update(name_split_uncertain=False, review_warnings=[])
    payload.update(overrides)
    return payload


class FakeGroqProvider:
    def __init__(
        self,
        *,
        transcript: str = "আমার নাম Arif Rahman, mobile 01712345678.",
        duration_seconds: float = 12.0,
        extraction: dict[str, Any] | None = None,
        transcription_error: Exception | None = None,
        extraction_error: Exception | None = None,
    ) -> None:
        self.transcript = transcript
        self.duration_seconds = duration_seconds
        self.extraction = extraction or voice_extraction()
        self.transcription_error = transcription_error
        self.extraction_error = extraction_error
        self.transcription_calls = 0
        self.extraction_calls = 0
        self.system_prompt = ""
        self.json_schema: dict[str, Any] = {}
        self.transcription_model = ""
        self.extraction_model = ""

    async def transcribe_audio(self, **kwargs: Any) -> GroqTranscription:
        self.transcription_calls += 1
        self.transcription_model = kwargs["model"]
        if self.transcription_error:
            raise self.transcription_error
        return GroqTranscription(
            text=self.transcript,
            duration_seconds=self.duration_seconds,
        )

    async def extract_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.extraction_calls += 1
        self.extraction_model = kwargs["model"]
        self.system_prompt = kwargs["system_prompt"]
        self.json_schema = kwargs["json_schema"]
        if self.extraction_error:
            raise self.extraction_error
        return deepcopy(self.extraction)


def authenticated_headers(
    database_session: Session,
    *,
    permission_key: PermissionKey | None,
    active_membership: bool = True,
    cross_tenant_token: bool = False,
) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:10]
    role = Role(
        key=f"voice-role-{suffix}",
        name=f"Voice Role {suffix}",
        tenant_id=None,
        is_system=True,
    )
    tenant = Tenant(name=f"Voice Tenant {suffix}", slug=f"voice-{suffix}")
    user = User(
        name="Voice User",
        login_id=f"voice.{suffix}",
        email=f"voice.{suffix}@example.com",
        password_hash=hash_password("voice-test-password"),
    )
    database_session.add_all([role, tenant, user])
    database_session.flush()
    if permission_key is not None:
        permission = Permission(
            key=permission_key.value,
            description=permission_key.value,
        )
        database_session.add(permission)
        database_session.flush()
        database_session.add(
            RolePermission(role_id=role.id, permission_id=permission.id)
        )
    membership = TenantMembership(
        tenant_id=tenant.id,
        user_id=user.id,
        role_id=role.id,
        is_active=active_membership,
    )
    database_session.add(membership)
    database_session.commit()
    token_tenant_id = tenant.id
    if cross_tenant_token:
        other_tenant = Tenant(
            name=f"Other Voice Tenant {suffix}",
            slug=f"other-voice-{suffix}",
        )
        database_session.add(other_tenant)
        database_session.commit()
        token_tenant_id = other_tenant.id
    token = create_access_token(
        user.id,
        user.email,
        scope="workspace",
        tenant_id=token_tenant_id,
        membership_id=membership.id,
    )
    return {"Authorization": f"Bearer {token}"}


def post_voice(
    client: TestClient,
    headers: dict[str, str],
    provider: object,
    *,
    content: bytes = b"mock audio bytes",
    filename: str = "lead.webm",
    content_type: str = "audio/webm",
):
    app.dependency_overrides[get_groq_provider] = lambda: provider
    try:
        return client.post(
            "/api/leads/voice-extract",
            headers=headers,
            files={"audio": (filename, content, content_type)},
        )
    finally:
        app.dependency_overrides.pop(get_groq_provider, None)


def test_voice_extract_bangla_mixed_leading_zero_and_missing_fields(
    client: TestClient,
    database_session: Session,
) -> None:
    headers = authenticated_headers(
        database_session, permission_key=PermissionKey.LEADS_VIEW
    )
    provider = FakeGroqProvider(
        extraction=voice_extraction(
            first_name="Arif",
            last_name="Rahman",
            email=" ARIF@EXAMPLE.COM ",
            phone="01712345678",
            country="Bangladesh",
            company_website="example.com",
            service_requested=" Website Design ",
        )
    )

    response = post_voice(client, headers, provider)

    assert response.status_code == 200
    body = response.json()
    assert body["transcript"].startswith("আমার নাম")
    assert body["extracted_fields"]["first_name"] == "Arif"
    assert body["extracted_fields"]["email"] == "arif@example.com"
    assert body["extracted_fields"]["phone"] == "01712345678"
    assert body["extracted_fields"]["country"] == "Bangladesh"
    assert body["extracted_fields"]["company_website"] == "https://example.com"
    assert body["extracted_fields"]["service_requested"] == "Website Design"
    assert body["extracted_fields"]["industry"] is None
    assert body["field_errors"] == []
    assert provider.transcription_model == "whisper-large-v3-turbo"
    assert provider.extraction_model == "openai/gpt-oss-20b"
    assert "mobile" in provider.system_prompt
    assert "untrusted data" in provider.system_prompt
    assert provider.json_schema["additionalProperties"] is False
    assert "service_requested" in provider.json_schema["required"]
    assert database_session.scalar(select(func.count(Lead.id))) == 0

    create_without_create_permission = client.post(
        "/api/leads",
        headers=headers,
        json={
            "first_name": "Voice",
            "last_name": "Only",
            "email": "voice-only@example.com",
        },
    )
    assert create_without_create_permission.status_code == 403


def test_voice_extract_flags_invalid_fields_and_uncertain_name(
    client: TestClient,
    database_session: Session,
) -> None:
    headers = authenticated_headers(
        database_session, permission_key=PermissionKey.LEADS_VIEW
    )
    provider = FakeGroqProvider(
        extraction=voice_extraction(
            first_name="Arif123",
            last_name=None,
            email="not-an-email",
            phone="01ABC",
            name_split_uncertain=True,
        )
    )

    response = post_voice(client, headers, provider)

    assert response.status_code == 200
    body = response.json()
    assert body["extracted_fields"]["first_name"] is None
    assert body["extracted_fields"]["email"] is None
    assert body["extracted_fields"]["phone"] is None
    assert {error["field"] for error in body["field_errors"]} == {
        "first_name",
        "email",
        "phone",
    }
    assert any("split is uncertain" in warning for warning in body["review_warnings"])


def test_voice_extract_denies_users_without_lead_menu_access(
    client: TestClient,
    database_session: Session,
) -> None:
    headers = authenticated_headers(
        database_session, permission_key=PermissionKey.LEADS_CREATE
    )
    provider = FakeGroqProvider()

    unauthenticated = post_voice(client, {}, provider)
    assert unauthenticated.status_code == 401
    assert provider.transcription_calls == 0

    response = post_voice(client, headers, provider)

    assert response.status_code == 403
    assert provider.transcription_calls == 0


@pytest.mark.parametrize(
    "membership_options",
    [
        {"active_membership": False},
        {"cross_tenant_token": True},
    ],
)
def test_voice_extract_requires_active_matching_tenant_membership(
    client: TestClient,
    database_session: Session,
    membership_options: dict[str, bool],
) -> None:
    headers = authenticated_headers(
        database_session,
        permission_key=PermissionKey.LEADS_VIEW,
        **membership_options,
    )
    provider = FakeGroqProvider()

    response = post_voice(client, headers, provider)

    assert response.status_code == 403
    assert provider.transcription_calls == 0


@pytest.mark.parametrize(
    ("provider", "expected_status"),
    [
        (
            FakeGroqProvider(transcription_error=GroqRateLimitError()),
            429,
        ),
        (
            FakeGroqProvider(transcription_error=GroqTimeoutError()),
            504,
        ),
        (
            FakeGroqProvider(transcription_error=GroqProviderError()),
            502,
        ),
        (
            FakeGroqProvider(extraction_error=GroqMalformedResponseError()),
            502,
        ),
    ],
)
def test_voice_extract_handles_provider_failures_safely(
    client: TestClient,
    database_session: Session,
    provider: FakeGroqProvider,
    expected_status: int,
) -> None:
    headers = authenticated_headers(
        database_session, permission_key=PermissionKey.LEADS_VIEW
    )

    response = post_voice(client, headers, provider)

    assert response.status_code == expected_status
    assert "mock audio" not in response.text
    assert "Groq" not in response.text


def test_voice_extract_handles_malformed_structured_output(
    client: TestClient,
    database_session: Session,
) -> None:
    headers = authenticated_headers(
        database_session, permission_key=PermissionKey.LEADS_VIEW
    )
    provider = FakeGroqProvider(extraction={"first_name": "Only one key"})

    response = post_voice(client, headers, provider)

    assert response.status_code == 502
    assert response.json()["detail"] == "Voice extraction provider is unavailable"


def test_voice_extract_rejects_bad_audio_silence_duration_and_size(
    client: TestClient,
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = authenticated_headers(
        database_session, permission_key=PermissionKey.LEADS_VIEW
    )
    provider = FakeGroqProvider()

    unsupported = post_voice(
        client,
        headers,
        provider,
        filename="lead.txt",
        content_type="text/plain",
    )
    assert unsupported.status_code == 415
    assert provider.transcription_calls == 0

    silent = post_voice(client, headers, FakeGroqProvider(transcript="   "))
    assert silent.status_code == 422

    too_long = post_voice(
        client,
        headers,
        FakeGroqProvider(duration_seconds=settings.lead_voice_max_duration_seconds + 1),
    )
    assert too_long.status_code == 422

    monkeypatch.setattr(settings, "lead_voice_max_upload_bytes", 3)
    too_large = post_voice(client, headers, provider, content=b"four")
    assert too_large.status_code == 413


def test_voice_extract_missing_key(
    client: TestClient,
    database_session: Session,
) -> None:
    headers = authenticated_headers(
        database_session, permission_key=PermissionKey.LEADS_VIEW
    )
    missing_key = post_voice(
        client,
        headers,
        GroqProvider(api_key=None, timeout_seconds=1),
    )
    assert missing_key.status_code == 503


def test_voice_extract_local_rate_limit(
    client: TestClient,
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = authenticated_headers(
        database_session, permission_key=PermissionKey.LEADS_VIEW
    )
    provider = FakeGroqProvider()
    monkeypatch.setattr(settings, "lead_voice_rate_limit_requests", 1)
    first = post_voice(client, headers, provider)
    second = post_voice(client, headers, provider)
    assert first.status_code == 200
    assert second.status_code == 429
