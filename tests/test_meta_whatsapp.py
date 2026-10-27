import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.core.permissions import RoleKey
from app.models.tenant import Tenant
from app.models.whatsapp import (
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppDeliveryEvent,
    WhatsAppMessage,
)
from app.providers.meta_whatsapp import (
    MetaWhatsAppAcceptanceUnknownError,
    MetaWhatsAppProvider,
    MetaWhatsAppRejectedError,
    MetaWhatsAppResult,
    get_meta_whatsapp_provider,
)
from tests.test_outbound_messages import setup


class MockMetaProvider:
    configured = True
    webhook_configured = True
    waba_id = "waba-test-001"
    phone_number_id = "phone-number-id-001"

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.result_id = "wamid.test.accepted.001"
        self.failure: Exception | None = None
        self.templates = [
            {
                "id": "template-001",
                "name": "lead_intro",
                "language": "en_US",
                "category": "MARKETING",
                "status": "APPROVED",
                "components": [
                    {"type": "BODY", "text": "Hi {{1}}, welcome to {{2}}"}
                ],
            },
            {
                "id": "template-002",
                "name": "pending_template",
                "language": "en_US",
                "category": "UTILITY",
                "status": "PENDING",
                "components": [{"type": "BODY", "text": "Pending"}],
            },
        ]

    def validate_signature(self, raw_body: bytes, signature: str) -> bool:
        return bool(raw_body) and signature == "sha256=valid"

    def validate_challenge(self, *, mode: str, verify_token: str) -> bool:
        return mode == "subscribe" and verify_token == "verify-test"

    async def list_templates(self, *, limit: int = 100):
        return self.templates[:limit]

    async def send_text(self, *, to: str, body: str) -> MetaWhatsAppResult:
        self.sent.append({"mode": "text", "to": to, "body": body})
        if self.failure:
            raise self.failure
        return MetaWhatsAppResult(message_id=self.result_id)

    async def send_template(self, *, to: str, name: str, language: str, components):
        self.sent.append(
            {
                "mode": "approved_template",
                "to": to,
                "name": name,
                "language": language,
                "components": components,
            }
        )
        if self.failure:
            raise self.failure
        return MetaWhatsAppResult(message_id=self.result_id)


def _channel(session: Session, tenant_id: uuid.UUID) -> WhatsAppChannel:
    channel = WhatsAppChannel(
        tenant_id=tenant_id,
        provider="meta",
        provider_account_id=MockMetaProvider.waba_id,
        provider_channel_id=MockMetaProvider.phone_number_id,
        twilio_account_sid=None,
        sender_address="+14155550000",
    )
    session.add(channel)
    session.commit()
    return channel


def _payload(*, message_id: str = "wamid.inbound.001", sender: str = "14155550123"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": MockMetaProvider.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {
                                "phone_number_id": MockMetaProvider.phone_number_id,
                                "display_phone_number": "+14155550000",
                            },
                            "messages": [
                                {
                                    "from": sender,
                                    "id": message_id,
                                    "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
                                    "type": "text",
                                    "text": {"body": "Hello from Meta"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _status_payload(*, message_id: str, state: str = "delivered", timestamp: str = "1792814401"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": MockMetaProvider.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {
                                "phone_number_id": MockMetaProvider.phone_number_id
                            },
                            "statuses": [
                                {
                                    "id": message_id,
                                    "status": state,
                                    "timestamp": timestamp,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _post_webhook(client: TestClient, payload: dict, signature: str = "sha256=valid"):
    return client.post(
        "/api/webhooks/meta/whatsapp",
        content=json.dumps(payload).encode(),
        headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
    )


def test_official_meta_signature_validation() -> None:
    provider = MetaWhatsAppProvider.__new__(MetaWhatsAppProvider)
    provider.app_secret = "test-app-secret"
    body = b'{"object":"whatsapp_business_account"}'
    digest = hmac.new(provider.app_secret.encode(), body, hashlib.sha256).hexdigest()
    assert provider.validate_signature(body, f"sha256={digest}")
    assert not provider.validate_signature(body + b"x", f"sha256={digest}")


def test_challenge_inbound_dedup_and_provider_isolation(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    _channel(database_session, tenant.id)
    provider = MockMetaProvider()
    app.dependency_overrides[get_meta_whatsapp_provider] = lambda: provider
    try:
        challenge = client.get(
            "/api/webhooks/meta/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-test",
                "hub.challenge": "challenge-123",
            },
        )
        invalid = _post_webhook(client, _payload(), "sha256=invalid")
        first = _post_webhook(client, _payload())
        duplicate = _post_webhook(client, _payload())
        meta_list = client.get(
            "/api/messages/meta/whatsapp/conversations", headers=headers
        )
        twilio_list = client.get(
            "/api/messages/whatsapp/conversations", headers=headers
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)

    assert challenge.status_code == 200 and challenge.text == "challenge-123"
    assert challenge.headers["content-type"].startswith("text/plain")
    assert invalid.status_code == 403
    assert first.status_code == duplicate.status_code == 200
    assert database_session.query(WhatsAppMessage).count() == 1
    assert meta_list.json()["data"]["total"] == 1
    assert meta_list.json()["data"]["items"][0]["external_address"] == "+14155550123"
    assert twilio_list.json()["data"]["total"] == 0


def test_open_does_not_send_and_tenant_isolation(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    _channel(database_session, tenant.id)
    other = Tenant(name="Other Meta", slug=f"other-meta-{uuid.uuid4().hex[:8]}")
    database_session.add(other)
    database_session.flush()
    other_channel = WhatsAppChannel(
        tenant_id=other.id,
        provider="meta",
        provider_account_id="other-waba",
        provider_channel_id="other-phone-id",
        sender_address="+14155550999",
    )
    database_session.add(other_channel)
    database_session.flush()
    hidden = WhatsAppConversation(
        tenant_id=other.id,
        channel_id=other_channel.id,
        external_address="+14155550888",
        last_message_at=datetime.now(timezone.utc),
    )
    database_session.add(hidden)
    database_session.commit()
    provider = MockMetaProvider()
    app.dependency_overrides[get_meta_whatsapp_provider] = lambda: provider
    try:
        opened = client.post(
            "/api/messages/meta/whatsapp/conversations/open",
            headers=headers,
            json={"phone_number": "+14155550155"},
        )
        reopened = client.post(
            "/api/messages/meta/whatsapp/conversations/open",
            headers=headers,
            json={"phone_number": "+14155550155"},
        )
        hidden_result = client.get(
            f"/api/messages/meta/whatsapp/conversations/{hidden.id}/messages",
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)
    assert opened.status_code == reopened.status_code == 200
    assert opened.json()["data"]["id"] == reopened.json()["data"]["id"]
    assert opened.json()["data"]["eligibility"]["free_form_allowed"] is False
    assert provider.sent == []
    assert hidden_result.status_code == 404


def test_text_send_idempotency_recovery_and_linked_activity(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, lead, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = WhatsAppConversation(
        tenant_id=tenant.id,
        channel_id=channel.id,
        external_address="+14155550123",
        lead_id=lead.id,
        last_message_at=datetime.now(timezone.utc),
        last_inbound_at=datetime.now(timezone.utc),
    )
    database_session.add(conversation)
    database_session.commit()
    provider = MockMetaProvider()
    app.dependency_overrides[get_meta_whatsapp_provider] = lambda: provider
    payload = {
        "conversation_id": str(conversation.id),
        "mode": "text",
        "text": "Thanks for messaging us",
        "purpose": "lead_acquisition",
        "idempotency_key": "meta-send-0001",
    }
    try:
        first = client.post(
            "/api/messages/meta/whatsapp/send", headers=headers, json=payload
        )
        replay = client.post(
            "/api/messages/meta/whatsapp/send", headers=headers, json=payload
        )
        conflict = client.post(
            "/api/messages/meta/whatsapp/send",
            headers=headers,
            json={**payload, "text": "Changed"},
        )
        recovery = client.get(
            "/api/messages/meta/whatsapp/operations/recover",
            headers=headers,
            params={"idempotency_key": "meta-send-0001"},
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)
    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["message"]["status"] == "accepted"
    assert replay.json()["data"]["replayed"] is True
    assert conflict.status_code == 409
    assert recovery.json()["data"]["operation_state"] == "completed"
    assert len(provider.sent) == 1
    assert provider.sent[0]["to"] == "+14155550123"


def test_closed_window_requires_approved_template_without_provider_call(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, lead, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = WhatsAppConversation(
        tenant_id=tenant.id,
        channel_id=channel.id,
        external_address="+14155550123",
        lead_id=lead.id,
        last_message_at=datetime.now(timezone.utc) - timedelta(days=2),
        last_inbound_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    database_session.add(conversation)
    database_session.commit()
    provider = MockMetaProvider()
    app.dependency_overrides[get_meta_whatsapp_provider] = lambda: provider
    try:
        response = client.post(
            "/api/messages/meta/whatsapp/send",
            headers=headers,
            json={
                "conversation_id": str(conversation.id),
                "mode": "text",
                "text": "Outside window",
                "purpose": "lead_acquisition",
                "idempotency_key": "meta-closed-001",
            },
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approved_template_required"
    assert provider.sent == []


def test_template_status_language_variables_and_opt_in(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, lead, _, _, _, headers = setup(client, database_session)
    lead.consent_status = "unknown"
    channel = _channel(database_session, tenant.id)
    conversation = WhatsAppConversation(
        tenant_id=tenant.id,
        channel_id=channel.id,
        external_address="+14155550123",
        lead_id=lead.id,
        last_message_at=datetime.now(timezone.utc) - timedelta(days=2),
        last_inbound_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    database_session.add(conversation)
    database_session.commit()
    provider = MockMetaProvider()
    app.dependency_overrides[get_meta_whatsapp_provider] = lambda: provider
    base = {
        "conversation_id": str(conversation.id),
        "mode": "approved_template",
        "purpose": "lead_acquisition",
        "template": {
            "name": "lead_intro",
            "language": "en_US",
            "parameters": [
                {"component": "body", "index": 1, "value": "Arif"},
                {"component": "body", "index": 2, "value": "PowerLead"},
            ],
        },
    }
    try:
        denied = client.post(
            "/api/messages/meta/whatsapp/send",
            headers=headers,
            json={**base, "idempotency_key": "meta-template-001"},
        )
        lead.consent_status = "granted"
        database_session.commit()
        invalid_variables = client.post(
            "/api/messages/meta/whatsapp/send",
            headers=headers,
            json={
                **base,
                "idempotency_key": "meta-template-002",
                "template": {
                    **base["template"],
                    "parameters": [
                        {"component": "body", "index": 1, "value": "Arif"}
                    ],
                },
            },
        )
        sent = client.post(
            "/api/messages/meta/whatsapp/send",
            headers=headers,
            json={**base, "idempotency_key": "meta-template-003"},
        )
        templates = client.get(
            "/api/messages/meta/whatsapp/templates", headers=headers
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "whatsapp_opt_in_required"
    assert invalid_variables.status_code == 409
    assert invalid_variables.json()["detail"]["code"] == "invalid_template_parameters"
    assert sent.status_code == 200
    assert sent.json()["data"]["message"]["mode"] == "approved_template"
    template_items = templates.json()["data"]["items"]
    assert template_items[0]["required_variables"] == [
        {"component": "body", "index": 1},
        {"component": "body", "index": 2},
    ]
    assert template_items[1]["usable"] is False


def test_callback_before_send_response_and_out_of_order_statuses(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, lead, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = WhatsAppConversation(
        tenant_id=tenant.id,
        channel_id=channel.id,
        external_address="+14155550123",
        lead_id=lead.id,
        last_message_at=datetime.now(timezone.utc),
        last_inbound_at=datetime.now(timezone.utc),
    )
    database_session.add(conversation)
    database_session.commit()
    provider = MockMetaProvider()
    provider.result_id = "wamid.callback.race.001"
    app.dependency_overrides[get_meta_whatsapp_provider] = lambda: provider
    try:
        callback = _post_webhook(
            client, _status_payload(message_id=provider.result_id, state="delivered")
        )
        sent = client.post(
            "/api/messages/meta/whatsapp/send",
            headers=headers,
            json={
                "conversation_id": str(conversation.id),
                "mode": "text",
                "text": "Race-safe",
                "purpose": "lead_acquisition",
                "idempotency_key": "meta-race-0001",
            },
        )
        stale = _post_webhook(
            client,
            _status_payload(
                message_id=provider.result_id,
                state="sent",
                timestamp="1792814300",
            ),
        )
        recovery = client.get(
            "/api/messages/meta/whatsapp/operations/recover",
            headers=headers,
            params={"idempotency_key": "meta-race-0001"},
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)
    assert callback.status_code == sent.status_code == stale.status_code == 200
    assert recovery.json()["data"]["message"]["status"] == "delivered"
    assert database_session.scalar(
        select(WhatsAppDeliveryEvent).where(
            WhatsAppDeliveryEvent.provider_message_sid == provider.result_id,
            WhatsAppDeliveryEvent.status == "delivered",
        )
    ).message_id is not None


def test_definitive_rejection_and_uncertain_outcome(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, lead, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = WhatsAppConversation(
        tenant_id=tenant.id,
        channel_id=channel.id,
        external_address="+14155550123",
        lead_id=lead.id,
        last_message_at=datetime.now(timezone.utc),
        last_inbound_at=datetime.now(timezone.utc),
    )
    database_session.add(conversation)
    database_session.commit()
    provider = MockMetaProvider()
    app.dependency_overrides[get_meta_whatsapp_provider] = lambda: provider
    try:
        provider.failure = MetaWhatsAppRejectedError(
            code="131030", safe_message="The recipient is not in the permitted test-recipient list"
        )
        rejected = client.post(
            "/api/messages/meta/whatsapp/send",
            headers=headers,
            json={
                "conversation_id": str(conversation.id),
                "mode": "text",
                "text": "Rejected",
                "purpose": "lead_acquisition",
                "idempotency_key": "meta-rejected-001",
            },
        )
        provider.failure = MetaWhatsAppAcceptanceUnknownError()
        unknown = client.post(
            "/api/messages/meta/whatsapp/send",
            headers=headers,
            json={
                "conversation_id": str(conversation.id),
                "mode": "text",
                "text": "Unknown",
                "purpose": "lead_acquisition",
                "idempotency_key": "meta-unknown-001",
            },
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)
    assert rejected.json()["data"]["message"]["status"] == "failed"
    assert rejected.json()["data"]["message"]["provider_error_code"] == "131030"
    assert unknown.json()["data"]["message"]["status"] == "unknown"
    assert unknown.json()["data"]["message"]["provider_error_code"] == "acceptance_unknown"



def test_readonly_role_has_no_meta_messages_permission(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(
        client, database_session, role_key=RoleKey.READONLY
    )
    _channel(database_session, tenant.id)
    response = client.get(
        "/api/messages/meta/whatsapp/conversations", headers=headers
    )
    assert response.status_code == 403
