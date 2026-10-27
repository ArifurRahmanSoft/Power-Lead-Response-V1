import uuid
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator

from app.main import app
from app.core.permissions import RoleKey
from app.models.lead_activity import LeadActivity
from app.models.whatsapp import (
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppConversationRead,
    WhatsAppDeliveryEvent,
    WhatsAppMessage,
)
from app.models.tenant import Tenant
from app.providers.twilio_whatsapp import (
    TwilioWhatsAppAcceptanceUnknownError,
    TwilioWhatsAppProvider,
    TwilioWhatsAppRejectedError,
    TwilioWhatsAppResult,
    get_twilio_whatsapp_provider,
)
from app.services.whatsapp_service import _persist_reply_outcome
from tests.test_outbound_messages import setup


class MockWhatsAppProvider:
    configured = True
    account_sid = "AC00000000000000000000000000000001"
    from_address = "whatsapp:+14155238886"
    webhook_base_url = "https://public.example.test"
    status_callback_url = (
        "https://public.example.test/api/webhooks/twilio/whatsapp/status"
    )

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.result_sid = "SM00000000000000000000000000000001"
        self.failure: Exception | None = None

    def validate_signature(self, **kwargs) -> bool:
        return kwargs["signature"] == "valid-signature"

    def send_text(self, *, to: str, body: str) -> TwilioWhatsAppResult:
        self.sent.append((to, body))
        if self.failure:
            raise self.failure
        return TwilioWhatsAppResult(sid=self.result_sid, status="queued")


def _channel(session: Session, tenant_id: uuid.UUID) -> WhatsAppChannel:
    channel = WhatsAppChannel(
        tenant_id=tenant_id,
        twilio_account_sid=MockWhatsAppProvider.account_sid,
        sender_address=MockWhatsAppProvider.from_address,
    )
    session.add(channel)
    session.commit()
    return channel


def _inbound_payload(*, sid: str = "SM-INBOUND-0001", sender: str = "+14155550123"):
    return {
        "AccountSid": MockWhatsAppProvider.account_sid,
        "MessageSid": sid,
        "From": f"whatsapp:{sender}",
        "To": MockWhatsAppProvider.from_address,
        "Body": "Hello from WhatsApp",
        "NumMedia": "0",
    }


def _post_inbound(client: TestClient, payload: dict[str, str], signature="valid-signature"):
    return client.post(
        "/api/webhooks/twilio/whatsapp/inbound",
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )


def test_official_signature_validator_uses_exact_external_url() -> None:
    provider = TwilioWhatsAppProvider.__new__(TwilioWhatsAppProvider)
    provider._auth_token = "test-auth-token"
    provider.webhook_base_url = "https://public.example.test"
    url = "https://public.example.test/api/webhooks/twilio/whatsapp/inbound?source=sandbox"
    values = {"AccountSid": "AC123", "MessageSid": "SM123"}
    signature = RequestValidator(provider._auth_token).compute_signature(url, values)

    assert provider.validate_signature(
        request_url="http://internal:8000/api/webhooks/twilio/whatsapp/inbound?source=sandbox",
        form_values=values,
        signature=signature,
    )
    assert not provider.validate_signature(
        request_url="http://internal:8000/api/webhooks/twilio/whatsapp/inbound?source=changed",
        form_values=values,
        signature=signature,
    )


def test_inbound_signature_mapping_dedup_unlinked_and_per_user_unread(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, lead, _, _, _, headers = setup(client, database_session)
    _channel(database_session, tenant.id)
    provider = MockWhatsAppProvider()
    app.dependency_overrides[get_twilio_whatsapp_provider] = lambda: provider
    try:
        denied = _post_inbound(client, _inbound_payload(), signature="invalid")
        first = _post_inbound(client, _inbound_payload())
        duplicate = _post_inbound(client, _inbound_payload())
        conversations = client.get(
            "/api/messages/whatsapp/conversations", headers=headers
        )
        conversation_id = conversations.json()["data"]["items"][0]["id"]
        marked = client.post(
            f"/api/messages/whatsapp/conversations/{conversation_id}/read",
            headers=headers,
        )
        linked = client.put(
            f"/api/messages/whatsapp/conversations/{conversation_id}/link",
            headers=headers,
            json={"lead_id": str(lead.id)},
        )
        after = client.get("/api/messages/whatsapp/conversations", headers=headers)
    finally:
        app.dependency_overrides.pop(get_twilio_whatsapp_provider, None)

    assert denied.status_code == 403
    assert first.status_code == duplicate.status_code == 200
    assert first.text.endswith("<Response></Response>")
    assert database_session.query(WhatsAppMessage).count() == 1
    item = conversations.json()["data"]["items"][0]
    assert item["lead_id"] is None and item["customer_id"] is None
    assert item["unread_count"] == 1
    assert item["free_form_reply_allowed"] is True
    assert marked.status_code == 200
    assert linked.status_code == 200
    assert linked.json()["data"]["lead_id"] == str(lead.id)
    assert after.json()["data"]["items"][0]["unread_count"] == 0
    assert database_session.query(WhatsAppConversationRead).count() == 1
    assert database_session.query(LeadActivity).count() == 1


def test_account_mapping_permissions_and_tenant_isolation(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    _channel(database_session, tenant.id)
    other_tenant = Tenant(name="Other WhatsApp", slug=f"other-wa-{uuid.uuid4().hex[:8]}")
    database_session.add(other_tenant)
    database_session.flush()
    other_channel = WhatsAppChannel(
        tenant_id=other_tenant.id,
        twilio_account_sid="AC00000000000000000000000000000002",
        sender_address="whatsapp:+14155238887",
    )
    database_session.add(other_channel)
    database_session.flush()
    other_conversation = WhatsAppConversation(
        tenant_id=other_tenant.id,
        channel_id=other_channel.id,
        external_address="whatsapp:+14155550199",
        last_message_at=datetime.now(timezone.utc),
        last_inbound_at=datetime.now(timezone.utc),
    )
    database_session.add(other_conversation)
    database_session.commit()
    provider = MockWhatsAppProvider()
    app.dependency_overrides[get_twilio_whatsapp_provider] = lambda: provider
    try:
        wrong_account = _post_inbound(
            client,
            {**_inbound_payload(sid="SM-WRONG-ACCOUNT"), "AccountSid": "AC-other"},
        )
        hidden = client.get(
            f"/api/messages/whatsapp/conversations/{other_conversation.id}/messages",
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_twilio_whatsapp_provider, None)
    assert wrong_account.status_code == 403
    assert hidden.status_code == 404


def test_readonly_role_has_no_messages_permission_by_default(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(
        client, database_session, role_key=RoleKey.READONLY
    )
    _channel(database_session, tenant.id)
    response = client.get("/api/messages/whatsapp/conversations", headers=headers)
    assert response.status_code == 403


def test_reply_idempotency_recovery_and_activity(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, lead, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = WhatsAppConversation(
        tenant_id=tenant.id,
        channel_id=channel.id,
        external_address="whatsapp:+14155550123",
        lead_id=lead.id,
        last_message_at=datetime.now(timezone.utc),
        last_inbound_at=datetime.now(timezone.utc),
        last_message_preview="Inbound",
    )
    database_session.add(conversation)
    database_session.commit()
    provider = MockWhatsAppProvider()
    app.dependency_overrides[get_twilio_whatsapp_provider] = lambda: provider
    payload = {"body": "Thanks for contacting us", "idempotency_key": "wa-reply-0001"}
    try:
        first = client.post(
            f"/api/messages/whatsapp/conversations/{conversation.id}/reply",
            headers=headers,
            json=payload,
        )
        replay = client.post(
            f"/api/messages/whatsapp/conversations/{conversation.id}/reply",
            headers=headers,
            json=payload,
        )
        conflict = client.post(
            f"/api/messages/whatsapp/conversations/{conversation.id}/reply",
            headers=headers,
            json={**payload, "body": "Changed"},
        )
        recovery = client.get(
            "/api/messages/whatsapp/operations/recover",
            headers=headers,
            params={"idempotency_key": payload["idempotency_key"]},
        )
    finally:
        app.dependency_overrides.pop(get_twilio_whatsapp_provider, None)

    assert first.status_code == replay.status_code == recovery.status_code == 200
    assert replay.json()["data"]["replayed"] is True
    assert conflict.status_code == 409
    assert len(provider.sent) == 1
    assert recovery.json()["data"]["found"] is True
    assert recovery.json()["data"]["message"]["status"] == "queued"
    message_id = first.json()["data"]["message"]["id"]
    activity = database_session.scalar(
        select(LeadActivity).where(
            LeadActivity.whatsapp_message_id == uuid.UUID(message_id)
        )
    )
    assert activity is not None and activity.channel == "whatsapp"


def test_expired_window_blocks_without_provider_call(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = WhatsAppConversation(
        tenant_id=tenant.id,
        channel_id=channel.id,
        external_address="whatsapp:+14155550123",
        last_message_at=datetime.now(timezone.utc) - timedelta(hours=25),
        last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    database_session.add(conversation)
    database_session.commit()
    provider = MockWhatsAppProvider()
    app.dependency_overrides[get_twilio_whatsapp_provider] = lambda: provider
    try:
        response = client.post(
            f"/api/messages/whatsapp/conversations/{conversation.id}/reply",
            headers=headers,
            json={"body": "Too late", "idempotency_key": "wa-expired-0001"},
        )
    finally:
        app.dependency_overrides.pop(get_twilio_whatsapp_provider, None)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approved_template_required"
    assert provider.sent == []


def test_rejection_and_ambiguous_acceptance_are_distinct_and_not_retried(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    now = datetime.now(timezone.utc)
    first = WhatsAppConversation(
        tenant_id=tenant.id,
        channel_id=channel.id,
        external_address="whatsapp:+14155550123",
        last_message_at=now,
        last_inbound_at=now,
    )
    second = WhatsAppConversation(
        tenant_id=tenant.id,
        channel_id=channel.id,
        external_address="whatsapp:+14155550124",
        last_message_at=now,
        last_inbound_at=now,
    )
    database_session.add_all([first, second])
    database_session.commit()
    provider = MockWhatsAppProvider()
    app.dependency_overrides[get_twilio_whatsapp_provider] = lambda: provider
    try:
        provider.failure = TwilioWhatsAppRejectedError(
            code="63016", safe_message="An approved WhatsApp template is required"
        )
        rejected = client.post(
            f"/api/messages/whatsapp/conversations/{first.id}/reply",
            headers=headers,
            json={"body": "One", "idempotency_key": "wa-reject-0001"},
        )
        provider.failure = TwilioWhatsAppAcceptanceUnknownError()
        unknown = client.post(
            f"/api/messages/whatsapp/conversations/{second.id}/reply",
            headers=headers,
            json={"body": "Two", "idempotency_key": "wa-unknown-0001"},
        )
    finally:
        app.dependency_overrides.pop(get_twilio_whatsapp_provider, None)

    assert rejected.json()["data"]["message"]["status"] == "failed"
    assert rejected.json()["data"]["message"]["provider_error_code"] == "63016"
    assert unknown.json()["data"]["message"]["status"] == "unknown"
    assert unknown.json()["data"]["message"]["provider_error_code"] == "acceptance_unknown"
    assert len(provider.sent) == 2


def test_callback_race_duplicate_and_out_of_order_events(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, _, _, _, _, _ = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = WhatsAppConversation(
        tenant_id=tenant.id,
        channel_id=channel.id,
        external_address="whatsapp:+14155550123",
        last_message_at=datetime.now(timezone.utc),
        last_inbound_at=datetime.now(timezone.utc),
    )
    database_session.add(conversation)
    database_session.flush()
    message = WhatsAppMessage(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction="outbound",
        body="Race",
        media_metadata=[],
        sender_address=channel.sender_address,
        recipient_address=conversation.external_address,
        status="unknown",
        submission_state="processing",
        idempotency_key="wa-race-0001",
        request_hash="0" * 64,
        requested_by_user_id=user.id,
        occurred_at=datetime.now(timezone.utc),
    )
    database_session.add(message)
    database_session.commit()
    provider = MockWhatsAppProvider()
    provider.result_sid = "SM-RACE-0001"
    app.dependency_overrides[get_twilio_whatsapp_provider] = lambda: provider
    base = {
        "AccountSid": provider.account_sid,
        "MessageSid": provider.result_sid,
        "From": provider.from_address,
        "To": conversation.external_address,
    }
    try:
        delivered = client.post(
            "/api/webhooks/twilio/whatsapp/status",
            data={**base, "MessageStatus": "delivered"},
            headers={"X-Twilio-Signature": "valid-signature"},
        )
        duplicate = client.post(
            "/api/webhooks/twilio/whatsapp/status",
            data={**base, "MessageStatus": "delivered"},
            headers={"X-Twilio-Signature": "valid-signature"},
        )
        persisted = _persist_reply_outcome(
            database_session,
            message_id=message.id,
            conversation_id=conversation.id,
            result=TwilioWhatsAppResult(sid=provider.result_sid, status="queued"),
            error=None,
        )
        older = client.post(
            "/api/webhooks/twilio/whatsapp/status",
            data={**base, "MessageStatus": "sent"},
            headers={"X-Twilio-Signature": "valid-signature"},
        )
    finally:
        app.dependency_overrides.pop(get_twilio_whatsapp_provider, None)

    database_session.refresh(persisted)
    assert delivered.status_code == duplicate.status_code == older.status_code == 200
    assert database_session.query(WhatsAppDeliveryEvent).count() == 2
    assert persisted.status == "delivered"
    assert all(
        event.message_id == persisted.id
        for event in database_session.scalars(select(WhatsAppDeliveryEvent)).all()
    )


def test_media_metadata_is_preserved_without_provider_urls(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    _channel(database_session, tenant.id)
    provider = MockWhatsAppProvider()
    payload = _inbound_payload(sid="SM-MEDIA-0001")
    payload.update(
        {
            "Body": "",
            "NumMedia": "1",
            "MediaContentType0": "image/jpeg",
            "MediaUrl0": "https://api.twilio.com/secret-authenticated-media",
        }
    )
    app.dependency_overrides[get_twilio_whatsapp_provider] = lambda: provider
    try:
        inbound = _post_inbound(client, payload)
        conversation_id = client.get(
            "/api/messages/whatsapp/conversations", headers=headers
        ).json()["data"]["items"][0]["id"]
        history = client.get(
            f"/api/messages/whatsapp/conversations/{conversation_id}/messages",
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_twilio_whatsapp_provider, None)

    assert inbound.status_code == 200
    item = history.json()["data"]["items"][0]
    assert item["body"] == "[Unsupported WhatsApp media]"
    assert item["media"] == [
        {"index": 0, "content_type": "image/jpeg", "unsupported": True}
    ]
    assert "MediaUrl" not in str(item)
