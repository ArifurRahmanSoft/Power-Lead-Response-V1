import asyncio
import hashlib
import hmac
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import app.routers.meta_messenger as messenger_router_module
import app.commands.map_meta_messenger_page as mapping_command
from app.core.sensitive_query import SensitiveQueryRedactionMiddleware
from app.main import app
from app.models.tenant import Tenant
from app.models.whatsapp import (
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppDeliveryEvent,
    WhatsAppMessage,
)
from app.providers.meta_messenger import (
    MetaMessengerAcceptanceUnknownError,
    MetaMessengerProvider,
    MetaMessengerPageProfile,
    MetaMessengerProfileUnavailableError,
    MetaMessengerRejectedError,
    MetaMessengerResult,
    MetaMessengerUserProfile,
    get_meta_messenger_provider,
)
from app.core.permissions import RoleKey
from tests.test_outbound_messages import setup


class MockMessengerProvider:
    page_id = "100000000000001"
    webhook_configured = True
    send_api_configured = True

    def __init__(
        self,
        *,
        error: Exception | None = None,
        profile_error: Exception | None = None,
        page_name: str = "PowerLead Page",
        user_name: str = "Arif Messenger",
    ) -> None:
        self.error = error
        self.profile_error = profile_error
        self.page_name = page_name
        self.user_name = user_name
        self.calls: list[dict[str, str | None]] = []
        self.page_profile_calls = 0
        self.user_profile_calls: list[str] = []

    def validate_signature(self, raw_body: bytes, signature: str) -> bool:
        return bool(raw_body) and signature == "sha256=valid"

    def validate_challenge(self, *, mode: str, verify_token: str) -> bool:
        return mode == "subscribe" and verify_token == "messenger-verify-test"

    async def send_text(self, *, to: str, body: str, metadata: str | None = None):
        self.calls.append({"to": to, "body": body, "metadata": metadata})
        if self.error:
            raise self.error
        return MetaMessengerResult(message_id="mid.outbound.api.001", recipient_id=to)

    async def get_page_profile(self):
        self.page_profile_calls += 1
        if self.profile_error:
            raise self.profile_error
        return MetaMessengerPageProfile(display_name=self.page_name)

    async def get_user_profile(self, *, psid: str):
        self.user_profile_calls.append(psid)
        if self.profile_error:
            raise self.profile_error
        return MetaMessengerUserProfile(display_name=self.user_name)


def _channel(
    database_session: Session,
    tenant_id: uuid.UUID,
    *,
    page_id: str = MockMessengerProvider.page_id,
) -> WhatsAppChannel:
    channel = WhatsAppChannel(
        tenant_id=tenant_id,
        provider="messenger",
        provider_account_id=page_id,
        provider_channel_id=page_id,
        twilio_account_sid=None,
        sender_address=page_id,
    )
    database_session.add(channel)
    database_session.commit()
    return channel


def _message_event(
    *,
    message_id: str = "mid.inbound.001",
    sender_id: str = "200000000000001",
    text: str = "Hello from Messenger",
) -> dict:
    return {
        "sender": {"id": sender_id},
        "recipient": {"id": MockMessengerProvider.page_id},
        "timestamp": 1792980000000,
        "message": {"mid": message_id, "text": text},
    }


def _payload(*events: dict, page_id: str = MockMessengerProvider.page_id) -> dict:
    return {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": 1792980000000,
                "messaging": list(events),
            }
        ],
    }


def _post(
    client: TestClient,
    payload: dict,
    signature: str = "sha256=valid",
):
    return client.post(
        "/api/webhooks/meta/messenger",
        content=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
        },
    )


def test_get_challenge_invalid_values_and_missing_parameters(
    client: TestClient,
) -> None:
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        valid = client.get(
            "/api/webhooks/meta/messenger",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "messenger-verify-test",
                "hub.challenge": "challenge-456",
            },
        )
        invalid_token = client.get(
            "/api/webhooks/meta/messenger",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token",
                "hub.challenge": "challenge-456",
            },
        )
        invalid_mode = client.get(
            "/api/webhooks/meta/messenger",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": "messenger-verify-test",
                "hub.challenge": "challenge-456",
            },
        )
        missing = client.get(
            "/api/webhooks/meta/messenger",
            params={"hub.mode": "subscribe", "hub.challenge": "challenge-456"},
        )
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert valid.status_code == 200
    assert valid.text == "challenge-456"
    assert valid.headers["content-type"].startswith("text/plain")
    assert invalid_token.status_code == invalid_mode.status_code == 403
    assert missing.status_code == 422


def test_verification_token_is_redacted_from_downstream_query_scope() -> None:
    captured: dict[str, object] = {}

    async def downstream(scope, receive, send):
        captured["query_string"] = scope["query_string"]
        captured["token"] = scope.get("state", {}).get("meta_webhook_verify_token")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = SensitiveQueryRedactionMiddleware(downstream)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/webhooks/meta/messenger",
        "query_string": b"hub.mode=subscribe&hub.verify_token=secret-value&hub.challenge=1",
        "state": {},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        del message

    asyncio.run(middleware(scope, receive, send))
    assert b"secret-value" not in captured["query_string"]
    assert b"%5BREDACTED%5D" in captured["query_string"]
    assert captured["token"] == "secret-value"


def test_official_meta_signature_validation() -> None:
    provider = MetaMessengerProvider.__new__(MetaMessengerProvider)
    provider.app_secret = "test-messenger-secret"
    body = b'{"object":"page"}'
    digest = hmac.new(provider.app_secret.encode(), body, hashlib.sha256).hexdigest()
    assert provider.validate_signature(body, f"sha256={digest}")
    assert not provider.validate_signature(body + b"x", f"sha256={digest}")


def test_separate_app_secret_precedence_and_shared_app_fallback() -> None:
    common = {
        "meta_messenger_page_id": MockMessengerProvider.page_id,
        "meta_messenger_page_access_token": SecretStr("page-token"),
        "meta_messenger_verify_token": SecretStr("verify-token"),
        "meta_app_secret": SecretStr("shared-secret"),
    }
    shared = MetaMessengerProvider(
        SimpleNamespace(**common, meta_messenger_app_secret=None)
    )
    separate = MetaMessengerProvider(
        SimpleNamespace(
            **common,
            meta_messenger_app_secret=SecretStr("separate-secret"),
        )
    )
    assert shared.app_secret == "shared-secret"
    assert separate.app_secret == "separate-secret"


def test_valid_signature_duplicate_messages_and_tenant_mapping(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, _, _, _, _, _ = setup(client, database_session)
    _channel(database_session, tenant.id)
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    payload = _payload(_message_event())
    try:
        invalid = _post(client, payload, signature="sha256=invalid")
        first = _post(client, payload)
        duplicate = _post(client, payload)
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert invalid.status_code == 403
    assert first.status_code == duplicate.status_code == 200
    messages = (
        database_session.query(WhatsAppMessage)
        .filter(WhatsAppMessage.provider == "messenger")
        .all()
    )
    assert len(messages) == 1
    assert messages[0].tenant_id == tenant.id
    assert messages[0].direction == "inbound"
    assert messages[0].sender_address == "200000000000001"
    assert messages[0].recipient_address == MockMessengerProvider.page_id
    assert database_session.query(WhatsAppConversation).count() == 1


def test_unknown_page_is_rejected_without_cross_tenant_persistence(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, _, _, _, _, _ = setup(client, database_session)
    _channel(database_session, tenant.id)
    other = Tenant(name="Other Messenger", slug=f"other-{uuid.uuid4().hex[:8]}")
    database_session.add(other)
    database_session.commit()
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        response = _post(
            client,
            _payload(_message_event(), page_id="999999999999999"),
        )
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert response.status_code == 403
    assert (
        database_session.query(WhatsAppMessage)
        .filter(WhatsAppMessage.provider == "messenger")
        .count()
        == 0
    )


def test_multiple_events_echo_delivery_read_and_unsupported(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, _, _, _, _, _ = setup(client, database_session)
    _channel(database_session, tenant.id)
    second = _message_event(
        message_id="mid.inbound.002",
        sender_id="200000000000002",
        text="Second message",
    )
    echo = {
        "sender": {"id": MockMessengerProvider.page_id},
        "recipient": {"id": "200000000000001"},
        "timestamp": 1792980001000,
        "message": {"mid": "mid.echo.001", "text": "Echo", "is_echo": True},
    }
    delivery = {
        "sender": {"id": "200000000000001"},
        "recipient": {"id": MockMessengerProvider.page_id},
        "timestamp": 1792980002000,
        "delivery": {"mids": ["mid.echo.001"], "watermark": 1792980002000},
    }
    read = {
        "sender": {"id": "200000000000001"},
        "recipient": {"id": MockMessengerProvider.page_id},
        "timestamp": 1792980003000,
        "read": {"watermark": 1792980003000},
    }
    unsupported = {
        "timestamp": 1792980004000,
        "postback": {"title": "Unsupported", "payload": "ignored"},
    }
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        response = _post(
            client,
            _payload(_message_event(), second, echo, delivery, read, unsupported),
        )
        duplicate_events = _post(
            client,
            _payload(echo, delivery, read, unsupported),
        )
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert response.status_code == 200
    assert duplicate_events.status_code == 200
    assert (
        database_session.query(WhatsAppMessage)
        .filter(WhatsAppMessage.provider == "messenger")
        .count()
        == 3
    )
    assert (
        database_session.query(WhatsAppDeliveryEvent)
        .filter(WhatsAppDeliveryEvent.provider == "messenger")
        .count()
        == 3
    )
    external = database_session.query(WhatsAppMessage).filter(WhatsAppMessage.body == "Echo").one()
    assert external.direction == "outbound"
    assert external.idempotency_key is None


def test_attachment_placeholder_does_not_persist_remote_url(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, _, _, _, _, _ = setup(client, database_session)
    _channel(database_session, tenant.id)
    event = _message_event(text="See attachment")
    event["message"]["attachments"] = [
        {
            "type": "image",
            "payload": {"url": "https://private.example/sensitive.jpg"},
        }
    ]
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        response = _post(client, _payload(event))
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert response.status_code == 200
    message = database_session.query(WhatsAppMessage).one()
    serialized = json.dumps(message.media_metadata)
    assert "private.example" not in serialized
    assert message.media_metadata == [{"type": "image", "unsupported": True}]
    assert "Unsupported Messenger image attachment" in message.body


def test_persistence_failure_returns_retryable_error(
    client: TestClient,
    monkeypatch,
) -> None:
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider

    def fail(*args, **kwargs):
        del args, kwargs
        raise SQLAlchemyError("simulated persistence failure")

    monkeypatch.setattr(
        messenger_router_module,
        "process_messenger_webhook_payload",
        fail,
    )
    try:
        response = _post(client, _payload(_message_event()))
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert response.status_code == 503
    assert response.json()["detail"] == "Meta Messenger webhook persistence failed"


def test_page_mapping_command_is_idempotent_and_refuses_reassignment(
    client: TestClient,
    database_session: Session,
    monkeypatch,
) -> None:
    tenant, _, _, _, _, _, _ = setup(client, database_session)

    class TestSessionLocal:
        @staticmethod
        @contextmanager
        def begin():
            try:
                yield database_session
                database_session.commit()
            except Exception:
                database_session.rollback()
                raise

    monkeypatch.setattr(
        mapping_command,
        "Settings",
        lambda: SimpleNamespace(
            meta_messenger_page_id=MockMessengerProvider.page_id
        ),
    )
    monkeypatch.setattr(mapping_command, "SessionLocal", TestSessionLocal)
    first, created = mapping_command.configure_page(tenant.slug)
    second, created_again = mapping_command.configure_page(tenant.slug)
    assert created is True
    assert created_again is False
    assert first.id == second.id

    other = Tenant(name="Other Page Owner", slug=f"owner-{uuid.uuid4().hex[:8]}")
    database_session.add(other)
    database_session.commit()
    with pytest.raises(RuntimeError, match="already mapped to another workspace"):
        mapping_command.configure_page(other.slug)


def _conversation(
    database_session: Session,
    tenant_id: uuid.UUID,
    channel: WhatsAppChannel,
    *,
    psid: str = "200000000000001",
    last_inbound_at: datetime | None = None,
) -> WhatsAppConversation:
    inbound = last_inbound_at or datetime.now(timezone.utc)
    conversation = WhatsAppConversation(
        tenant_id=tenant_id,
        channel_id=channel.id,
        external_address=psid,
        last_message_at=inbound,
        last_inbound_at=inbound,
        last_message_preview="Hello",
    )
    database_session.add(conversation)
    database_session.flush()
    database_session.add(WhatsAppMessage(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        direction="inbound",
        provider="messenger",
        message_mode="text",
        body="Hello",
        media_metadata=[],
        sender_address=psid,
        recipient_address=channel.sender_address,
        provider_message_sid=f"mid.{uuid.uuid4().hex}",
        status="received",
        submission_state="received",
        occurred_at=inbound,
        completed_at=inbound,
    ))
    database_session.commit()
    return conversation


def test_authenticated_inbox_history_and_per_user_read(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = _conversation(database_session, tenant.id, channel)
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        channel_response = client.get("/api/messages/meta/messenger/channel", headers=headers)
        page = client.get("/api/messages/meta/messenger/conversations?page=1&size=10", headers=headers)
        history = client.get(
            f"/api/messages/meta/messenger/conversations/{conversation.id}/messages?page=1&size=10",
            headers=headers,
        )
        read = client.post(
            f"/api/messages/meta/messenger/conversations/{conversation.id}/read",
            headers=headers,
        )
        after = client.get("/api/messages/meta/messenger/conversations", headers=headers)
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert channel_response.status_code == 200
    assert "page_access_token" not in channel_response.text
    assert page.status_code == history.status_code == read.status_code == 200
    assert page.json()["data"]["items"][0]["contact_display_name"] == "Messenger user"
    assert page.json()["data"]["items"][0]["unread_count"] == 1
    assert page.json()["data"]["items"][0]["eligibility"]["reply_allowed"] is True
    assert history.json()["data"]["total"] == 1
    assert after.json()["data"]["items"][0]["unread_count"] == 0


def test_reply_idempotency_conflict_and_recovery_never_resends(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = _conversation(database_session, tenant.id, channel)
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    path = f"/api/messages/meta/messenger/conversations/{conversation.id}/reply"
    try:
        first = client.post(path, headers=headers, json={"text": "Reply text", "idempotency_key": "messenger-reply-001"})
        replay = client.post(path, headers=headers, json={"text": "Reply text", "idempotency_key": "messenger-reply-001"})
        conflict = client.post(path, headers=headers, json={"text": "Different text", "idempotency_key": "messenger-reply-001"})
        recovered = client.get(
            "/api/messages/meta/messenger/operations/recover",
            headers=headers,
            params={"idempotency_key": "messenger-reply-001"},
        )
        missing = client.get(
            "/api/messages/meta/messenger/operations/recover",
            headers=headers,
            params={"idempotency_key": "messenger-missing-001"},
        )
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert first.status_code == replay.status_code == recovered.status_code == missing.status_code == 200
    assert first.json()["data"]["message"]["status"] == "accepted"
    assert replay.json()["data"]["replayed"] is True
    assert conflict.status_code == 409
    assert recovered.json()["data"]["operation_state"] == "completed"
    assert missing.json()["data"]["operation_state"] == "missing"
    assert len(provider.calls) == 1
    assert provider.calls[0]["to"] == conversation.external_address
    assert provider.calls[0]["metadata"] == first.json()["data"]["message"]["id"]


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (MetaMessengerRejectedError(code="190", safe_message="expired"), "failed", "190"),
        (MetaMessengerAcceptanceUnknownError(), "unknown", "acceptance_unknown"),
    ],
)
def test_provider_failure_states_are_persisted(
    client: TestClient,
    database_session: Session,
    error: Exception,
    expected_status: str,
    expected_code: str,
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = _conversation(database_session, tenant.id, channel)
    provider = MockMessengerProvider(error=error)
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        response = client.post(
            f"/api/messages/meta/messenger/conversations/{conversation.id}/reply",
            headers=headers,
            json={"text": "Reply", "idempotency_key": f"failure-{expected_status}-001"},
        )
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert response.status_code == 200
    message = response.json()["data"]["message"]
    assert message["status"] == expected_status
    assert message["provider_error_code"] == expected_code


def test_expired_window_permissions_and_tenant_isolation(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    expired = _conversation(
        database_session, tenant.id, channel,
        last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    other = Tenant(name="Other Messenger Tenant", slug=f"other-{uuid.uuid4().hex[:8]}")
    database_session.add(other)
    database_session.flush()
    other_channel = _channel(database_session, other.id, page_id="999999999999998")
    hidden = _conversation(database_session, other.id, other_channel, psid="299999999999999")
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        closed = client.post(
            f"/api/messages/meta/messenger/conversations/{expired.id}/reply",
            headers=headers,
            json={"text": "Too late", "idempotency_key": "expired-window-001"},
        )
        isolated = client.get(
            f"/api/messages/meta/messenger/conversations/{hidden.id}/messages",
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert closed.status_code == 409
    assert closed.json()["detail"]["code"] == "messenger_reply_window_closed"
    assert isolated.status_code == 404
    assert provider.calls == []


def test_readonly_role_cannot_access_messenger_inbox(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session, role_key=RoleKey.READONLY)
    _channel(database_session, tenant.id)
    response = client.get("/api/messages/meta/messenger/conversations", headers=headers)
    assert response.status_code == 403


def test_echo_reconciles_processing_intent_and_watermarks_advance_state(
    client: TestClient, database_session: Session
) -> None:
    tenant, user, _, _, _, _, _ = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = _conversation(database_session, tenant.id, channel)
    intent = WhatsAppMessage(
        tenant_id=tenant.id, conversation_id=conversation.id, direction="outbound",
        provider="messenger", message_mode="text", body="Race reply", media_metadata=[],
        sender_address=channel.sender_address, recipient_address=conversation.external_address,
        status="unknown", submission_state="processing", idempotency_key="race-reply-001",
        request_hash="a" * 64, requested_by_user_id=user.id,
        occurred_at=datetime.now(timezone.utc),
    )
    database_session.add(intent)
    database_session.commit()
    echo = {
        "sender": {"id": MockMessengerProvider.page_id},
        "recipient": {"id": conversation.external_address},
        "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        "message": {"mid": "mid.race.001", "text": "Race reply", "metadata": str(intent.id), "is_echo": True},
    }
    read = {
        "sender": {"id": conversation.external_address},
        "recipient": {"id": MockMessengerProvider.page_id},
        "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        "read": {"watermark": int((datetime.now(timezone.utc) + timedelta(seconds=1)).timestamp() * 1000)},
    }
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        response = _post(client, _payload(echo, read))
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    database_session.refresh(intent)
    assert response.status_code == 200
    assert intent.provider_message_sid == "mid.race.001"
    assert intent.status == "read"
    assert database_session.query(WhatsAppMessage).filter(
        WhatsAppMessage.provider_message_sid == "mid.race.001"
    ).count() == 1


def test_profile_refresh_caches_page_and_contact_names(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = _conversation(database_session, tenant.id, channel)
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        first = client.post(
            "/api/messages/meta/messenger/profiles/refresh",
            headers=headers,
            json={"conversation_ids": [str(conversation.id)], "refresh_page": True},
        )
        inbox = client.get(
            "/api/messages/meta/messenger/conversations", headers=headers
        )
        channel_response = client.get(
            "/api/messages/meta/messenger/channel", headers=headers
        )
        cached = client.post(
            "/api/messages/meta/messenger/profiles/refresh",
            headers=headers,
            json={"conversation_ids": [str(conversation.id)], "refresh_page": True},
        )
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert first.status_code == inbox.status_code == channel_response.status_code == cached.status_code == 200
    assert first.json()["data"]["page_status"] == "refreshed"
    assert first.json()["data"]["items"][0]["status"] == "refreshed"
    item = inbox.json()["data"]["items"][0]
    assert item["contact_display_name"] == "Arif Messenger"
    assert item["contact_display_name_source"] == "provider_profile"
    assert item["page_display_name"] == "PowerLead Page"
    assert channel_response.json()["data"]["page_display_name"] == "PowerLead Page"
    assert cached.json()["data"]["page_status"] == "cached"
    assert cached.json()["data"]["items"][0]["status"] == "cached"
    assert provider.page_profile_calls == 1
    assert provider.user_profile_calls == [conversation.external_address]


def test_restricted_profile_uses_backoff_and_safe_fallback(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = _conversation(database_session, tenant.id, channel)
    provider = MockMessengerProvider(profile_error=MetaMessengerProfileUnavailableError(
        code="200", safe_message="Profile permission is unavailable"
    ))
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        first = client.post(
            "/api/messages/meta/messenger/profiles/refresh",
            headers=headers,
            json={"conversation_ids": [str(conversation.id)]},
        )
        second = client.post(
            "/api/messages/meta/messenger/profiles/refresh",
            headers=headers,
            json={"conversation_ids": [str(conversation.id)]},
        )
        inbox = client.get("/api/messages/meta/messenger/conversations", headers=headers)
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert first.status_code == second.status_code == inbox.status_code == 200
    assert first.json()["data"]["items"][0]["status"] == "unavailable"
    assert first.json()["data"]["items"][0]["reason_code"] == "200"
    assert second.json()["data"]["items"][0]["status"] == "unavailable"
    assert provider.page_profile_calls == 1
    assert provider.user_profile_calls == [conversation.external_address]
    item = inbox.json()["data"]["items"][0]
    assert item["contact_display_name"] == "Messenger user"
    assert item["contact_display_name_source"] == "fallback"
    assert item["page_display_name"] == "Facebook Page"


def test_refresh_does_not_override_linked_contact_name(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, lead, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = _conversation(database_session, tenant.id, channel)
    conversation.lead_id = lead.id
    database_session.commit()
    provider = MockMessengerProvider(user_name="Different Meta Name")
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        refreshed = client.post(
            "/api/messages/meta/messenger/profiles/refresh",
            headers=headers,
            json={"conversation_ids": [str(conversation.id)], "refresh_page": False},
        )
        inbox = client.get("/api/messages/meta/messenger/conversations", headers=headers)
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert refreshed.status_code == inbox.status_code == 200
    item = inbox.json()["data"]["items"][0]
    assert item["contact_display_name"] == "Arif Rahman"
    assert item["contact_display_name_source"] == "linked_record"


def test_profile_refresh_preserves_tenant_isolation(
    client: TestClient, database_session: Session
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    _channel(database_session, tenant.id)
    other = Tenant(name="Other Profile Tenant", slug=f"profile-{uuid.uuid4().hex[:8]}")
    database_session.add(other)
    database_session.flush()
    other_channel = _channel(database_session, other.id, page_id="999999999999997")
    hidden = _conversation(database_session, other.id, other_channel, psid="299999999999997")
    provider = MockMessengerProvider()
    app.dependency_overrides[get_meta_messenger_provider] = lambda: provider
    try:
        response = client.post(
            "/api/messages/meta/messenger/profiles/refresh",
            headers=headers,
            json={"conversation_ids": [str(hidden.id)]},
        )
    finally:
        app.dependency_overrides.pop(get_meta_messenger_provider, None)
    assert response.status_code == 404
    assert provider.page_profile_calls == 0
    assert provider.user_profile_calls == []
