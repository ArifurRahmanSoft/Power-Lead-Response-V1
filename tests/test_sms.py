import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from twilio.base.exceptions import TwilioRestException

from app.main import app
from app.models.lead import Lead
from app.models.lead_activity import LeadActivity
from app.models.message_template import MessageTemplate
from app.models.sms_message import SmsBatch, SmsBatchRecipient, SmsMessage, SmsMessageEvent
from app.models.tenant import Tenant
from app.providers.twilio_sms import (
    TwilioSmsAcceptanceUnknownError,
    TwilioSmsProvider,
    TwilioSmsRejectedError,
    TwilioSmsResult,
    get_twilio_sms_provider,
    safe_twilio_rejection_message,
)
from tests.test_outbound_messages import setup
from app.core.permissions import RoleKey
import app.routers.sms as sms_router_module
import app.services.sms_service as sms_service_module


class MockTwilioProvider:
    configured = True
    from_number = "+15005550006"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.lookups: list[str] = []
        self.status_by_phone: dict[str, str] = {}
        self.lookup_status = "delivered"

    def send_message(self, *, to: str, body: str) -> TwilioSmsResult:
        self.sent.append((to, body))
        outcome = self.status_by_phone.get(to, "queued")
        if outcome == "failed":
            raise TwilioSmsRejectedError(
                code="21408",
                safe_message="Twilio geographic permissions do not allow this destination",
            )
        if outcome == "unknown":
            raise TwilioSmsAcceptanceUnknownError
        return TwilioSmsResult(
            sid=f"SM{len(self.sent):032d}",
            status=outcome,
            date_created=datetime.now(timezone.utc),
        )

    def fetch_message(self, message_sid: str) -> TwilioSmsResult:
        self.lookups.append(message_sid)
        return TwilioSmsResult(sid=message_sid, status=self.lookup_status)


class UnconfiguredTwilioProvider:
    configured = False
    from_number = ""


class RejectingTwilioProvider:
    configured = True
    from_number = "+15005550006"

    def __init__(self, code: str, http_status: int = 400) -> None:
        self.code = code
        self.http_status = http_status

    def send_message(self, *, to: str, body: str) -> TwilioSmsResult:
        raise TwilioSmsRejectedError(
            code=self.code,
            http_status=self.http_status,
            safe_message=safe_twilio_rejection_message(self.code),
        )


def _template(session: Session, tenant_id, user_id, body: str) -> MessageTemplate:
    template = MessageTemplate(
        tenant_id=tenant_id,
        name=f"SMS {uuid.uuid4().hex[:8]}",
        normalized_name=f"sms-{uuid.uuid4().hex}",
        body=body,
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
    )
    session.add(template)
    session.commit()
    return template


def _request(lead_ids, template_id, key="sms-send-0001"):
    return {
        "lead_ids": [str(value) for value in lead_ids],
        "message_template_id": str(template_id),
        "contact_type": "initial",
        "idempotency_key": key,
    }


@pytest.mark.parametrize(
    ("code", "expected_reason"),
    [
        (
            21608,
            "Twilio account restrictions allow SMS only to verified recipients; "
            "verify this recipient in Twilio or complete the required account upgrade "
            "and compliance profile",
        ),
        (
            21610,
            "The recipient has opted out; obtain renewed consent and have them opt in "
            "before sending again",
        ),
        (
            21408,
            "Twilio Messaging Geographic Permissions block this destination; enable the "
            "supported region or correct the country code",
        ),
        (
            21612,
            "Twilio cannot send using the current sender and recipient combination; "
            "verify E.164 formatting and the destination's sender-type and network "
            "restrictions",
        ),
        (
            63038,
            "Twilio account has reached its daily messaging limit; wait for the rolling "
            "24-hour allowance to reset or review the account messaging limit",
        ),
    ],
)
def test_twilio_provider_preserves_code_status_and_safe_rejection(
    monkeypatch, code: int, expected_reason: str
) -> None:
    provider = TwilioSmsProvider()
    provider_message = "raw provider detail containing a phone number and request URI"

    class FailingMessages:
        def create(self, **kwargs):
            raise TwilioRestException(
                status=400,
                uri="/Accounts/AC-secret/Messages.json",
                msg=provider_message,
                code=code,
                method="POST",
                details={"to": "+14155550123"},
            )

    monkeypatch.setattr(
        provider,
        "_client",
        lambda: SimpleNamespace(messages=FailingMessages()),
    )

    with pytest.raises(TwilioSmsRejectedError) as raised:
        provider.send_message(to="+14155550123", body="Test")

    error = raised.value
    assert error.code == str(code)
    assert error.http_status == 400
    assert error.safe_message == expected_reason
    assert provider_message not in error.safe_message
    assert "AC-secret" not in error.safe_message


@pytest.mark.parametrize(
    ("code", "expected_fragment"),
    [
        ("21608", "verified recipients"),
        ("21610", "opted out"),
        ("21408", "Geographic Permissions"),
        ("63038", "daily messaging limit"),
    ],
)
def test_distinct_twilio_rejections_are_persisted_and_returned_safely(
    client: TestClient,
    database_session: Session,
    code: str,
    expected_fragment: str,
) -> None:
    tenant, user, lead, _, _, _, headers = setup(client, database_session)
    lead.phone = "+14155552671"
    template = _template(database_session, tenant.id, user.id, "Hi")
    provider = RejectingTwilioProvider(code)
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    try:
        response = client.post(
            "/api/sms/send",
            headers=headers,
            json=_request([lead.id], template.id, f"sms-rejection-{code}"),
        )
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)

    assert response.status_code == 200, response.text
    recipient_result = response.json()["data"]["recipients"][0]
    assert recipient_result["reason_code"] == code
    assert expected_fragment in recipient_result["reason"]

    message = database_session.scalar(
        select(SmsMessage).where(SmsMessage.tenant_id == tenant.id)
    )
    assert message is not None
    recipient = database_session.scalar(
        select(SmsBatchRecipient).where(
            SmsBatchRecipient.sms_message_id == message.id
        )
    )
    event = database_session.scalar(
        select(SmsMessageEvent)
        .where(SmsMessageEvent.sms_message_id == message.id)
        .order_by(SmsMessageEvent.occurred_at.desc())
    )
    assert message.provider_error_code == code
    assert expected_fragment in message.failure_reason
    assert recipient is not None and recipient.reason_code == code
    assert expected_fragment in recipient.reason
    assert event is not None and event.provider_error_code == code
    assert expected_fragment in event.safe_reason

    generic_reason = "Twilio rejected the SMS request; check provider restrictions"
    message.failure_reason = generic_reason
    recipient.reason = generic_reason
    event.safe_reason = generic_reason
    database_session.commit()

    batch_response = client.get(
        f"/api/sms/batches/{message.batch_id}", headers=headers
    )
    message_response = client.get(f"/api/sms/messages/{message.id}", headers=headers)
    assert batch_response.status_code == message_response.status_code == 200
    assert expected_fragment in batch_response.json()["data"]["recipients"][0]["reason"]
    assert expected_fragment in message_response.json()["data"]["failure_reason"]
    assert expected_fragment in message_response.json()["data"]["events"][-1]["safe_reason"]


def test_single_sms_personalizes_unicode_and_records_activity(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, lead, _, _, _, headers = setup(client, database_session)
    lead.phone = "+8801712345678"
    template = _template(
        database_session,
        tenant.id,
        user.id,
        "হ্যালো {{first_name}},\nআপনার সেবার বিষয়ে কথা বলতে চাই।",
    )
    provider = MockTwilioProvider()
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    try:
        response = client.post(
            "/api/sms/send",
            headers=headers,
            json=_request([lead.id], template.id),
        )
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["queued_count"] == 1
    assert data["delivered_count"] == 0
    assert provider.sent == [
        ("+8801712345678", "হ্যালো Arif,\nআপনার সেবার বিষয়ে কথা বলতে চাই।")
    ]
    message = database_session.scalar(select(SmsMessage))
    assert message is not None
    assert message.rendered_body.startswith("হ্যালো Arif")
    assert message.provider_message_sid.startswith("SM")
    activity = database_session.scalar(
        select(LeadActivity).where(LeadActivity.sms_message_id == message.id)
    )
    assert activity is not None
    assert (activity.channel, activity.outcome, activity.contact_stage) == (
        "sms",
        "queued",
        "initial",
    )


def test_bulk_sms_mixed_results_and_duplicate_recipient(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, lead, _, _, _, headers = setup(client, database_session)
    lead.phone = "+8801711111111"
    leads = [lead]
    for index, phone in enumerate(
        [
            "+8801722222222",
            "01733333333",
            "+8801711111111",
            "+8801744444444",
        ],
        start=2,
    ):
        item = Lead(
            tenant_id=tenant.id,
            tracking_id=f"LD-SMS-{uuid.uuid4().hex[:10]}",
            first_name=f"Lead{index}",
            last_name="Test",
            email=f"sms-{uuid.uuid4().hex[:8]}@example.com",
            email_original=f"sms-{uuid.uuid4().hex[:8]}@example.com",
            phone=phone,
            email_status="valid",
            source="manual",
            status="new",
            priority="normal",
            consent_status="unknown",
            assigned_user_id=user.id,
            created_by=user.id,
            updated_by=user.id,
        )
        database_session.add(item)
        leads.append(item)
    database_session.commit()
    template = _template(database_session, tenant.id, user.id, "Hi {{first_name}}")
    provider = MockTwilioProvider()
    provider.status_by_phone["+8801722222222"] = "failed"
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    try:
        response = client.post(
            "/api/sms/send",
            headers=headers,
            json=_request([item.id for item in leads], template.id, "sms-mixed-001"),
        )
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["failed_count"] == 1
    assert data["unknown_count"] == 0
    assert data["skipped_count"] == 3
    assert data["queued_count"] == 1
    assert {item["reason_code"] for item in data["recipients"]} >= {
        "invalid_phone",
        "duplicate_recipient",
        "21408",
    }
    assert set(provider.sent) == {
        ("+8801722222222", "Hi Lead2"),
        ("+8801744444444", "Hi Lead5"),
    }


def test_sms_idempotency_and_payload_conflict(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, lead, _, _, _, headers = setup(client, database_session)
    lead.phone = "+14155552671"
    template = _template(database_session, tenant.id, user.id, "Hi {{first_name}}")
    provider = MockTwilioProvider()
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    request = _request([lead.id], template.id, "same-sms-request")
    try:
        first = client.post("/api/sms/send", headers=headers, json=request)
        replay = client.post("/api/sms/send", headers=headers, json=request)
        conflict = client.post(
            "/api/sms/send",
            headers=headers,
            json={**request, "contact_type": "follow_up"},
        )
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)

    assert first.status_code == replay.status_code == 200
    assert replay.json()["data"]["replayed"] is True
    assert first.json()["data"]["batch_id"] == replay.json()["data"]["batch_id"]
    assert conflict.status_code == 409
    assert len(provider.sent) == 1
    assert database_session.query(SmsBatch).count() == 1


def test_sms_permissions_tenant_scope_and_denied_consent(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, lead, _, _, _, headers = setup(
        client, database_session, role_key=RoleKey.READONLY
    )
    lead.phone = "+14155552671"
    template = _template(database_session, tenant.id, user.id, "Hello")
    provider = MockTwilioProvider()
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    try:
        denied = client.post(
            "/api/sms/send",
            headers=headers,
            json=_request([lead.id], template.id, "sms-readonly-1"),
        )
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)
    assert denied.status_code == 403
    assert provider.sent == []


def test_sms_tenant_scope_and_denied_consent_are_skipped(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, lead, _, _, _, headers = setup(client, database_session)
    lead.phone = "+14155552671"
    lead.consent_status = "denied"
    other_tenant = Tenant(
        name="Other SMS Workspace", slug=f"sms-other-{uuid.uuid4().hex[:8]}"
    )
    database_session.add(other_tenant)
    database_session.flush()
    other_lead = Lead(
        tenant_id=other_tenant.id,
        tracking_id=f"LD-SMS-{uuid.uuid4().hex[:10]}",
        first_name="Other",
        last_name="Tenant",
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        phone="+14155552672",
        email_status="valid",
        source="manual",
        status="new",
        priority="normal",
        consent_status="granted",
        created_by=user.id,
        updated_by=user.id,
    )
    database_session.add(other_lead)
    database_session.commit()
    template = _template(database_session, tenant.id, user.id, "Hi {{first_name}}")
    provider = MockTwilioProvider()
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    try:
        response = client.post(
            "/api/sms/send",
            headers=headers,
            json=_request(
                [lead.id, other_lead.id], template.id, "sms-scope-denied-1"
            ),
        )
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)

    assert response.status_code == 200, response.text
    recipients = response.json()["data"]["recipients"]
    assert response.json()["data"]["skipped_count"] == 2
    assert {item["reason_code"] for item in recipients} == {
        "sms_consent_denied",
        "lead_unavailable",
    }
    assert provider.sent == []


def test_sms_status_refresh_and_lead_list_summary(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, lead, _, _, _, headers = setup(client, database_session)
    lead.phone = "+14155552671"
    template = _template(database_session, tenant.id, user.id, "Hi {{first_name}}")
    provider = MockTwilioProvider()
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    try:
        sent = client.post(
            "/api/sms/send",
            headers=headers,
            json=_request([lead.id], template.id, "sms-refresh-1"),
        )
        message_id = sent.json()["data"]["recipients"][0]["message_id"]
        refreshed = client.post(
            f"/api/sms/messages/{message_id}/refresh-status", headers=headers
        )
        throttled = client.post(
            f"/api/sms/messages/{message_id}/refresh-status", headers=headers
        )
        lead_list = client.get("/api/leads", headers=headers)
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)

    assert refreshed.status_code == throttled.status_code == 200
    assert refreshed.json()["data"]["status"] == "delivered"
    assert throttled.json()["refreshed"] is False
    assert len(provider.lookups) == 1
    summary = lead_list.json()["data"]["items"][0]["sms_summary"]
    assert summary["provider_status"] == "delivered"
    assert summary["contact_type"] == "initial"
    assert summary["last_successful_at"] is not None


def test_configuration_failure_is_pre_provider_and_recovery_is_missing(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, lead, _, _, _, headers = setup(client, database_session)
    lead.phone = "+14155552671"
    template = _template(database_session, tenant.id, user.id, "Hi")
    provider = UnconfiguredTwilioProvider()
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    try:
        response = client.post(
            "/api/sms/send",
            headers=headers,
            json=_request([lead.id], template.id, "sms-config-missing"),
        )
        recovery = client.get(
            "/api/sms/operations/recover",
            headers=headers,
            params={"idempotency_key": "sms-config-missing"},
        )
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "sms_configuration_unavailable",
        "classification": "pre_provider_failure",
        "provider_request_made": False,
        "message": "Twilio SMS is not configured in the running API process",
    }
    assert database_session.query(SmsBatch).count() == 0
    assert recovery.status_code == 200
    assert recovery.json()["data"] == {
        "idempotency_key": "sms-config-missing",
        "found": False,
        "operation_state": "missing",
        "batch": None,
    }


def test_uncertain_provider_acceptance_is_not_retried_and_is_recoverable(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, lead, _, _, _, headers = setup(client, database_session)
    lead.phone = "+14155552671"
    template = _template(database_session, tenant.id, user.id, "Hi")
    provider = MockTwilioProvider()
    provider.status_by_phone[lead.phone] = "unknown"
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    try:
        response = client.post(
            "/api/sms/send",
            headers=headers,
            json=_request([lead.id], template.id, "sms-unknown-result"),
        )
        recovery = client.get(
            "/api/sms/operations/recover",
            headers=headers,
            params={"idempotency_key": "sms-unknown-result"},
        )
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)

    assert response.status_code == recovery.status_code == 200
    assert response.json()["data"]["unknown_count"] == 1
    assert recovery.json()["data"]["found"] is True
    assert recovery.json()["data"]["operation_state"] == "completed"
    assert recovery.json()["data"]["batch"]["unknown_count"] == 1
    assert len(provider.sent) == 1


def test_recovery_reports_in_progress_without_sending(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, _, _, _, _, headers = setup(client, database_session)
    template = _template(database_session, tenant.id, user.id, "Hi")
    batch = SmsBatch(
        tenant_id=tenant.id,
        message_template_id=template.id,
        template_updated_at=template.updated_at,
        contact_type="initial",
        idempotency_key="sms-still-processing",
        request_hash="0" * 64,
        selection_count=1,
        status="processing",
        created_by_user_id=user.id,
    )
    database_session.add(batch)
    database_session.commit()

    recovery = client.get(
        "/api/sms/operations/recover",
        headers=headers,
        params={"idempotency_key": "sms-still-processing"},
    )

    assert recovery.status_code == 200, recovery.text
    assert recovery.json()["data"]["operation_state"] == "in_progress"
    assert recovery.json()["data"]["batch"]["status"] == "processing"


def test_transient_persistence_failure_retries_database_not_twilio(
    client: TestClient, database_session: Session, monkeypatch
) -> None:
    tenant, user, lead, _, _, _, headers = setup(client, database_session)
    lead.phone = "+14155552671"
    template = _template(database_session, tenant.id, user.id, "Hi")
    provider = MockTwilioProvider()
    original = sms_service_module._apply_provider_outcome
    attempts = 0

    def fail_first_persistence(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SQLAlchemyError("simulated transient persistence failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        sms_service_module, "_apply_provider_outcome", fail_first_persistence
    )
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    try:
        response = client.post(
            "/api/sms/send",
            headers=headers,
            json=_request([lead.id], template.id, "sms-db-retry-001"),
        )
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)

    assert response.status_code == 200, response.text
    assert attempts == 2
    assert len(provider.sent) == 1
    message = database_session.scalar(
        select(SmsMessage).where(SmsMessage.lead_id == lead.id)
    )
    database_session.refresh(message)
    assert message.status == "queued"
    assert message.provider_message_sid is not None


def test_response_failure_recovers_accepted_result_by_idempotency_key(
    client: TestClient, database_session: Session, monkeypatch
) -> None:
    tenant, user, lead, _, _, _, headers = setup(client, database_session)
    lead.phone = "+14155552671"
    template = _template(database_session, tenant.id, user.id, "Hi")
    provider = MockTwilioProvider()
    original = sms_router_module._batch_data
    calls = 0

    def fail_first_response(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated response construction failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(sms_router_module, "_batch_data", fail_first_response)
    app.dependency_overrides[get_twilio_sms_provider] = lambda: provider
    try:
        with pytest.raises(RuntimeError, match="response construction"):
            client.post(
                "/api/sms/send",
                headers=headers,
                json=_request([lead.id], template.id, "sms-response-lost"),
            )
        recovery = client.get(
            "/api/sms/operations/recover",
            headers=headers,
            params={"idempotency_key": "sms-response-lost"},
        )
    finally:
        app.dependency_overrides.pop(get_twilio_sms_provider, None)

    assert len(provider.sent) == 1
    assert recovery.status_code == 200, recovery.text
    recovered = recovery.json()["data"]
    assert recovered["found"] is True
    assert recovered["operation_state"] == "completed"
    assert recovered["batch"]["queued_count"] == 1
    assert recovered["batch"]["recipients"][0]["provider_message_sid"]
