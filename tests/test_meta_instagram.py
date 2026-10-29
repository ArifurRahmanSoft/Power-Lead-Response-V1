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
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import app.commands.map_meta_instagram_account as mapping_command
import app.routers.meta_instagram as instagram_router_module
from app.core.permissions import RoleKey
from app.core.sensitive_query import SensitiveQueryRedactionMiddleware
from app.main import app
from app.models.tenant import Tenant
from app.models.membership import TenantMembership
from app.models.role import Role
from app.models.whatsapp import (
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppDeliveryEvent,
    WhatsAppMessage,
)
from app.providers.meta_instagram import (
    MetaInstagramAcceptanceUnknownError,
    MetaInstagramProvider,
    MetaInstagramRejectedError,
    MetaInstagramResult,
    get_meta_instagram_provider,
)
from app.services.meta_instagram_service import process_instagram_webhook_payload
from tests.test_outbound_messages import setup


class MockInstagramProvider:
    configuration_id = "1392416376006210"
    account_id = "17841400000000001"
    webhook_configured = True
    send_api_configured = True

    def __init__(self, *, error: Exception | None = None, callback=None) -> None:
        self.error = error
        self.callback = callback
        self.calls: list[dict[str, str]] = []

    def validate_signature(self, raw_body: bytes, signature: str) -> bool:
        return bool(raw_body) and signature == "sha256=valid"

    def validate_challenge(self, *, mode: str, verify_token: str) -> bool:
        return mode == "subscribe" and verify_token == "instagram-verify-test"

    async def send_text(self, *, to: str, body: str) -> MetaInstagramResult:
        self.calls.append({"to": to, "body": body})
        if self.callback is not None:
            self.callback()
        if self.error is not None:
            raise self.error
        return MetaInstagramResult(
            message_id="mid.ig.outbound.api.001",
            recipient_id=to,
        )


def _channel(
    database_session: Session,
    tenant_id: uuid.UUID,
    *,
    account_id: str = MockInstagramProvider.account_id,
) -> WhatsAppChannel:
    channel = WhatsAppChannel(
        tenant_id=tenant_id,
        provider="instagram",
        provider_account_id=account_id,
        provider_channel_id=account_id,
        twilio_account_sid=None,
        sender_address=account_id,
        display_name="PowerLead Instagram",
    )
    database_session.add(channel)
    database_session.commit()
    return channel


def _message_event(
    *,
    message_id: str = "mid.ig.inbound.001",
    sender_id: str = "18000000000000001",
    text: str = "Hello from Instagram",
) -> dict:
    return {
        "sender": {"id": sender_id},
        "recipient": {"id": MockInstagramProvider.account_id},
        "timestamp": 1792980000000,
        "message": {"mid": message_id, "text": text},
    }


def _payload(*events: dict, account_id: str = MockInstagramProvider.account_id) -> dict:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": account_id,
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
        "/api/webhooks/meta/instagram",
        content=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
        },
    )


def _conversation(
    database_session: Session,
    tenant_id: uuid.UUID,
    channel: WhatsAppChannel,
    *,
    igsid: str = "18000000000000001",
    last_inbound_at: datetime | None = None,
) -> WhatsAppConversation:
    inbound = last_inbound_at or datetime.now(timezone.utc)
    conversation = WhatsAppConversation(
        tenant_id=tenant_id,
        channel_id=channel.id,
        external_address=igsid,
        last_message_at=inbound,
        last_inbound_at=inbound,
        last_message_preview="Hello",
    )
    database_session.add(conversation)
    database_session.flush()
    database_session.add(
        WhatsAppMessage(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            direction="inbound",
            provider="instagram",
            message_mode="text",
            body="Hello",
            media_metadata=[],
            sender_address=igsid,
            recipient_address=channel.sender_address,
            provider_message_sid=f"mid.ig.{uuid.uuid4().hex}",
            status="received",
            submission_state="received",
            occurred_at=inbound,
            completed_at=inbound,
        )
    )
    database_session.commit()
    return conversation


def test_challenge_invalid_values_and_query_token_redaction(client: TestClient) -> None:
    provider = MockInstagramProvider()
    app.dependency_overrides[get_meta_instagram_provider] = lambda: provider
    try:
        valid = client.get(
            "/api/webhooks/meta/instagram",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "instagram-verify-test",
                "hub.challenge": "challenge-ig-123",
            },
        )
        invalid = client.get(
            "/api/webhooks/meta/instagram",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "challenge-ig-123",
            },
        )
        missing = client.get(
            "/api/webhooks/meta/instagram",
            params={"hub.mode": "subscribe", "hub.challenge": "challenge-ig-123"},
        )
    finally:
        app.dependency_overrides.pop(get_meta_instagram_provider, None)
    assert valid.status_code == 200
    assert valid.text == "challenge-ig-123"
    assert valid.headers["content-type"].startswith("text/plain")
    assert invalid.status_code == 403
    assert missing.status_code == 422

    captured: dict[str, object] = {}

    async def downstream(scope, receive, send):
        del receive
        captured["query_string"] = scope["query_string"]
        captured["token"] = scope.get("state", {}).get("meta_webhook_verify_token")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = SensitiveQueryRedactionMiddleware(downstream)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/webhooks/meta/instagram",
        "query_string": b"hub.mode=subscribe&hub.verify_token=secret-value&hub.challenge=1",
        "state": {},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        del message

    asyncio.run(middleware(scope, receive, send))
    assert b"secret-value" not in captured["query_string"]
    assert captured["token"] == "secret-value"


def test_signature_validation_and_shared_app_secret_fallback() -> None:
    provider = MetaInstagramProvider.__new__(MetaInstagramProvider)
    provider.app_secret = "test-instagram-secret"
    body = b'{"object":"instagram"}'
    digest = hmac.new(provider.app_secret.encode(), body, hashlib.sha256).hexdigest()
    assert provider.validate_signature(body, f"sha256={digest}")
    assert not provider.validate_signature(body + b"x", f"sha256={digest}")

    common = {
        "meta_instagram_config_id": MockInstagramProvider.configuration_id,
        "meta_instagram_account_id": MockInstagramProvider.account_id,
        "meta_instagram_access_token": SecretStr("bisu-token"),
        "meta_instagram_verify_token": SecretStr("verify-token"),
        "meta_app_secret": SecretStr("shared-secret"),
        "meta_graph_api_version": "v26.0",
        "meta_instagram_timeout_seconds": 20.0,
    }
    shared = MetaInstagramProvider(
        SimpleNamespace(**common, meta_instagram_app_secret=None)
    )
    separate = MetaInstagramProvider(
        SimpleNamespace(
            **common,
            meta_instagram_app_secret=SecretStr("separate-secret"),
        )
    )
    assert shared.app_secret == "shared-secret"
    assert separate.app_secret == "separate-secret"
    assert shared.base_url == "https://graph.facebook.com/v26.0"


def test_webhook_dedup_account_isolation_echo_read_and_safe_media(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, _, _, _, _, _ = setup(client, database_session)
    _channel(database_session, tenant.id)
    inbound = _message_event()
    inbound["sender"]["username"] = "arif.instagram"
    attachment = _message_event(
        message_id="mid.ig.inbound.attachment",
        sender_id="18000000000000002",
        text="Photo",
    )
    attachment["message"]["attachments"] = [
        {"type": "image", "payload": {"url": "https://private.example/photo.jpg"}}
    ]
    echo = {
        "sender": {"id": MockInstagramProvider.account_id},
        "recipient": {"id": "18000000000000001"},
        "timestamp": 1792980001000,
        "message": {"mid": "mid.ig.echo.001", "text": "Echo", "is_echo": True},
    }
    read = {
        "sender": {"id": "18000000000000001"},
        "recipient": {"id": MockInstagramProvider.account_id},
        "timestamp": 1792980002000,
        "read": {"mid": "mid.ig.echo.001"},
    }
    unsupported = {"timestamp": 1792980003000, "postback": {"payload": "ignored"}}
    provider = MockInstagramProvider()
    app.dependency_overrides[get_meta_instagram_provider] = lambda: provider
    try:
        invalid_signature = _post(client, _payload(inbound), "sha256=invalid")
        first = _post(client, _payload(inbound, attachment, echo, read, unsupported))
        duplicate = _post(client, _payload(inbound, attachment, echo, read, unsupported))
        unknown = _post(client, _payload(inbound, account_id="17841499999999999"))
    finally:
        app.dependency_overrides.pop(get_meta_instagram_provider, None)

    assert invalid_signature.status_code == 403
    assert first.status_code == duplicate.status_code == 200
    assert unknown.status_code == 403
    messages = database_session.scalars(
        select(WhatsAppMessage).where(WhatsAppMessage.provider == "instagram")
    ).all()
    assert len(messages) == 3
    assert len([item for item in messages if item.direction == "inbound"]) == 2
    named_conversation = database_session.scalar(
        select(WhatsAppConversation).where(
            WhatsAppConversation.external_address == inbound["sender"]["id"]
        )
    )
    assert named_conversation is not None
    assert named_conversation.provider_display_name == "arif.instagram"
    echo_record = next(item for item in messages if item.provider_message_sid == "mid.ig.echo.001")
    assert echo_record.direction == "outbound"
    assert echo_record.status == "read"
    media_record = next(item for item in messages if item.provider_message_sid == "mid.ig.inbound.attachment")
    assert media_record.media_metadata == [{"type": "image", "unsupported": True}]
    assert "private.example" not in json.dumps(media_record.media_metadata)
    assert database_session.query(WhatsAppDeliveryEvent).count() == 2


def test_persistence_failure_is_retryable(client: TestClient, monkeypatch) -> None:
    provider = MockInstagramProvider()
    app.dependency_overrides[get_meta_instagram_provider] = lambda: provider

    def fail(*args, **kwargs):
        del args, kwargs
        raise SQLAlchemyError("simulated persistence failure")

    monkeypatch.setattr(
        instagram_router_module,
        "process_instagram_webhook_payload",
        fail,
    )
    try:
        response = _post(client, _payload(_message_event()))
    finally:
        app.dependency_overrides.pop(get_meta_instagram_provider, None)
    assert response.status_code == 503
    assert response.json()["detail"] == "Meta Instagram webhook persistence failed"


def test_authenticated_inbox_history_crm_read_and_permissions(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, user, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = _conversation(database_session, tenant.id, channel)
    provider = MockInstagramProvider()
    app.dependency_overrides[get_meta_instagram_provider] = lambda: provider
    try:
        readiness = client.get("/api/messages/meta/instagram/channel", headers=headers)
        inbox = client.get("/api/messages/meta/instagram/conversations", headers=headers)
        history = client.get(
            f"/api/messages/meta/instagram/conversations/{conversation.id}/messages",
            headers=headers,
        )
        marked = client.post(
            f"/api/messages/meta/instagram/conversations/{conversation.id}/read",
            headers=headers,
        )
        after = client.get("/api/messages/meta/instagram/conversations", headers=headers)
    finally:
        app.dependency_overrides.pop(get_meta_instagram_provider, None)
    assert readiness.status_code == inbox.status_code == history.status_code == marked.status_code == 200
    assert "access_token" not in readiness.text
    assert readiness.json()["data"]["configuration_id"] == MockInstagramProvider.configuration_id
    item = inbox.json()["data"]["items"][0]
    assert item["contact_display_name"] == "Instagram user"
    assert item["unread_count"] == 1
    assert item["eligibility"]["reply_allowed"] is True
    assert history.json()["data"]["total"] == 1
    assert marked.json()["data"]["provider_receipt_sent"] is False
    assert after.json()["data"]["items"][0]["unread_count"] == 0

    membership = database_session.scalar(
        select(TenantMembership).where(
            TenantMembership.tenant_id == tenant.id,
            TenantMembership.user_id == user.id,
        )
    )
    readonly_role = database_session.scalar(
        select(Role).where(Role.key == RoleKey.READONLY.value)
    )
    assert membership is not None and readonly_role is not None
    membership.role_id = readonly_role.id
    database_session.commit()
    denied = client.get(
        "/api/messages/meta/instagram/conversations",
        headers=headers,
    )
    assert denied.status_code == 403


def test_reply_unicode_idempotency_conflict_recovery_and_window(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = _conversation(database_session, tenant.id, channel)
    expired = _conversation(
        database_session,
        tenant.id,
        channel,
        igsid="18000000000000009",
        last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    provider = MockInstagramProvider()
    app.dependency_overrides[get_meta_instagram_provider] = lambda: provider
    request = {"text": "ধন্যবাদ\nWe received your message.", "idempotency_key": "ig-reply-0001"}
    path = f"/api/messages/meta/instagram/conversations/{conversation.id}/reply"
    try:
        first = client.post(path, headers=headers, json=request)
        replay = client.post(path, headers=headers, json=request)
        conflict = client.post(
            path,
            headers=headers,
            json={"text": "Different", "idempotency_key": "ig-reply-0001"},
        )
        recovery = client.get(
            "/api/messages/meta/instagram/operations/recover",
            headers=headers,
            params={"idempotency_key": "ig-reply-0001"},
        )
        missing = client.get(
            "/api/messages/meta/instagram/operations/recover",
            headers=headers,
            params={"idempotency_key": "ig-missing-0001"},
        )
        closed = client.post(
            f"/api/messages/meta/instagram/conversations/{expired.id}/reply",
            headers=headers,
            json={"text": "Too late", "idempotency_key": "ig-expired-0001"},
        )
    finally:
        app.dependency_overrides.pop(get_meta_instagram_provider, None)
    assert first.status_code == replay.status_code == recovery.status_code == missing.status_code == 200
    assert first.json()["data"]["message"]["status"] == "accepted"
    assert replay.json()["data"]["replayed"] is True
    assert conflict.status_code == 409
    assert recovery.json()["data"]["operation_state"] == "completed"
    assert missing.json()["data"]["operation_state"] == "missing"
    assert closed.status_code == 409
    assert closed.json()["detail"]["code"] == "instagram_reply_window_closed"
    assert provider.calls == [{"to": conversation.external_address, "body": request["text"]}]


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (MetaInstagramRejectedError(code="190", safe_message="expired"), "failed", "190"),
        (MetaInstagramAcceptanceUnknownError(), "unknown", "acceptance_unknown"),
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
    provider = MockInstagramProvider(error=error)
    app.dependency_overrides[get_meta_instagram_provider] = lambda: provider
    try:
        response = client.post(
            f"/api/messages/meta/instagram/conversations/{conversation.id}/reply",
            headers=headers,
            json={
                "text": "Reply",
                "idempotency_key": f"ig-failure-{expected_status}",
            },
        )
    finally:
        app.dependency_overrides.pop(get_meta_instagram_provider, None)
    assert response.status_code == 200
    message = response.json()["data"]["message"]
    assert message["status"] == expected_status
    assert message["provider_error_code"] == expected_code


def test_callback_before_response_reconciles_without_status_downgrade(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    channel = _channel(database_session, tenant.id)
    conversation = _conversation(database_session, tenant.id, channel)

    provider = MockInstagramProvider()

    def callback() -> None:
        echo = {
            "sender": {"id": provider.account_id},
            "recipient": {"id": conversation.external_address},
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "message": {
                "mid": "mid.ig.outbound.api.001",
                "text": "Race reply",
                "is_echo": True,
            },
        }
        read = {
            "sender": {"id": conversation.external_address},
            "recipient": {"id": provider.account_id},
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000) + 1,
            "read": {"mid": "mid.ig.outbound.api.001"},
        }
        process_instagram_webhook_payload(
            database_session,
            provider,
            _payload(echo, read),
        )

    provider.callback = callback
    app.dependency_overrides[get_meta_instagram_provider] = lambda: provider
    try:
        response = client.post(
            f"/api/messages/meta/instagram/conversations/{conversation.id}/reply",
            headers=headers,
            json={"text": "Race reply", "idempotency_key": "ig-race-reply-0001"},
        )
    finally:
        app.dependency_overrides.pop(get_meta_instagram_provider, None)
    assert response.status_code == 200
    assert response.json()["data"]["message"]["status"] == "read"
    assert database_session.query(WhatsAppMessage).filter(
        WhatsAppMessage.provider_message_sid == "mid.ig.outbound.api.001"
    ).count() == 1


def test_tenant_record_scope_and_mapping_command(
    client: TestClient,
    database_session: Session,
    monkeypatch,
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    _channel(database_session, tenant.id)
    other = Tenant(name="Other Instagram Tenant", slug=f"ig-{uuid.uuid4().hex[:8]}")
    database_session.add(other)
    database_session.flush()
    other_channel = _channel(
        database_session,
        other.id,
        account_id="17841499999999998",
    )
    hidden = _conversation(
        database_session,
        other.id,
        other_channel,
        igsid="18000000000000999",
    )
    response = client.get(
        f"/api/messages/meta/instagram/conversations/{hidden.id}/messages",
        headers=headers,
    )
    assert response.status_code == 404

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
        lambda: SimpleNamespace(meta_instagram_account_id=MockInstagramProvider.account_id),
    )
    monkeypatch.setattr(mapping_command, "SessionLocal", TestSessionLocal)
    first, created = mapping_command.configure_account(tenant.slug)
    second, created_again = mapping_command.configure_account(tenant.slug)
    assert first.id == second.id
    assert created is False
    assert created_again is False
    with pytest.raises(RuntimeError, match="already mapped to another workspace"):
        mapping_command.configure_account(other.slug)
