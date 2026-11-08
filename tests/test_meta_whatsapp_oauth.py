import asyncio
from datetime import datetime, timedelta, timezone
import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
import app.providers.meta_whatsapp as meta_whatsapp_provider_module
from app.core.config import Settings
from app.core.permissions import RoleKey
from app.models.meta_whatsapp_oauth import MetaWhatsAppOAuthOperation
from app.models.tenant import Tenant
from app.models.whatsapp import WhatsAppChannel
from app.providers.meta_whatsapp import (
    MetaWhatsAppConfigurationError,
    MetaWhatsAppDiscoveredAssets,
    MetaWhatsAppOAuthConfigurationDiagnostic,
    MetaWhatsAppOAuthError,
    MetaWhatsAppOAuthToken,
    MetaWhatsAppProvider,
    MetaWhatsAppSettingDiagnostic,
    get_meta_whatsapp_provider,
)
from app.services.meta_whatsapp_service import provider_for_channel
from app.utils.credential_encryption import decrypt_credential
from tests.test_outbound_messages import setup


class MockEmbeddedSignupProvider:
    embedded_signup_config_id = "222222222222222"
    oauth_redirect_uri = "https://api.example.test/meta/whatsapp/oauth"
    app_id = "111111111111111"
    graph_api_version = "v26.0"

    def __init__(self) -> None:
        self.exchange_calls = 0
        self.discovery_calls = 0
        self.subscription_calls = 0
        self.subscription_failure: Exception | None = None
        self.token = "synthetic-business-token"
        self.assets = MetaWhatsAppDiscoveredAssets(
            business_id="333333333333333",
            waba_id="444444444444444",
            phone_number_id="555555555555555",
            sender_address="+14155550000",
            display_name="Fixture Business",
            is_on_biz_app=True,
            platform_type="CLOUD_API",
        )

    def oauth_configuration_diagnostic(self):
        settings = (
            MetaWhatsAppSettingDiagnostic("META_WHATSAPP_APP_ID", True, True),
            MetaWhatsAppSettingDiagnostic(
                "META_WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID", True, True
            ),
            MetaWhatsAppSettingDiagnostic(
                "META_WHATSAPP_EMBEDDED_SIGNUP_VERSION", True, True
            ),
            MetaWhatsAppSettingDiagnostic(
                "META_WHATSAPP_OAUTH_REDIRECT_URI", True, True
            ),
            MetaWhatsAppSettingDiagnostic("META_APP_SECRET", True, True),
            MetaWhatsAppSettingDiagnostic(
                "SMTP_CREDENTIAL_ENCRYPTION_KEY", True, True
            ),
            MetaWhatsAppSettingDiagnostic("META_GRAPH_API_VERSION", True, True),
        )
        return MetaWhatsAppOAuthConfigurationDiagnostic(
            settings=settings,
            missing_settings=(),
            invalid_settings=(),
            ready=True,
        )

    def embedded_signup_options(self):
        return {
            "app_id": self.app_id,
            "configuration_id": self.embedded_signup_config_id,
            "graph_api_version": self.graph_api_version,
            "response_type": "code",
            "override_default_response_type": True,
            "extras": {
                "setup": {},
                "sessionInfoVersion": "3",
                "featureType": "whatsapp_business_app_onboarding",
                "version": "v4",
            },
        }

    async def exchange_embedded_signup_code(self, code: str):
        self.exchange_calls += 1
        assert code == "synthetic-short-lived-code"
        return MetaWhatsAppOAuthToken(
            access_token=self.token,
            token_type="bearer",
            expires_in=3600,
        )

    async def discover_coexistence_assets(self, **kwargs):
        self.discovery_calls += 1
        assert kwargs["access_token"] == self.token
        assert kwargs["waba_id"] == self.assets.waba_id
        assert kwargs["phone_number_id"] == self.assets.phone_number_id
        return self.assets

    async def subscribe_waba(self, **kwargs):
        self.subscription_calls += 1
        assert kwargs == {
            "access_token": self.token,
            "waba_id": self.assets.waba_id,
        }
        if self.subscription_failure:
            raise self.subscription_failure


def _settings(**overrides):
    values = {
        "live": False,
        "local_database_url": "postgresql://fixture:fixture@localhost/fixture",
        "production_database_url": "postgresql://fixture:fixture@db.example/fixture",
        "jwt_secret_key": "fixture-jwt-secret-that-is-long-enough-0001",
        "smtp_credential_encryption_key": Fernet.generate_key().decode("ascii"),
        "meta_whatsapp_app_id": "111111111111111",
        "meta_whatsapp_embedded_signup_config_id": "222222222222222",
        "meta_whatsapp_embedded_signup_version": "v4",
        "meta_whatsapp_oauth_redirect_uri": "https://api.example.test/meta/whatsapp/oauth",
        "meta_app_secret": "fixture-app-secret",
        "meta_graph_api_version": "v26.0",
    }
    values.update(overrides)
    return Settings(**values)


def _complete_payload(start: dict[str, object], provider: MockEmbeddedSignupProvider):
    return {
        "operation_id": start["operation_id"],
        "state": start["state"],
        "code": "synthetic-short-lived-code",
        "session": {
            "event": "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
            "business_id": provider.assets.business_id,
            "waba_id": provider.assets.waba_id,
            "phone_number_id": provider.assets.phone_number_id,
        },
    }


def _initiate(client: TestClient, headers: dict[str, str], provider):
    app.dependency_overrides[get_meta_whatsapp_provider] = lambda: provider
    return client.post(
        "/api/messages/meta/whatsapp/connect/initiate", headers=headers
    )


def test_embedded_signup_authorization_completion_and_replay(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    provider = MockEmbeddedSignupProvider()
    app.dependency_overrides[get_meta_whatsapp_provider] = lambda: provider
    try:
        readiness = client.get(
            "/api/messages/meta/whatsapp/connect/readiness", headers=headers
        )
        started = _initiate(client, headers, provider)
        start = started.json()["data"]
        completed = client.post(
            "/api/messages/meta/whatsapp/connect/complete",
            headers=headers,
            json=_complete_payload(start, provider),
        )
        replay = client.post(
            "/api/messages/meta/whatsapp/connect/complete",
            headers=headers,
            json=_complete_payload(start, provider),
        )
        recovered = client.get(
            f"/api/messages/meta/whatsapp/connect/operations/{start['operation_id']}",
            headers=headers,
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)

    assert readiness.status_code == 200
    assert readiness.json()["data"]["oauth_configuration_ready"] is True
    assert started.status_code == 201
    assert start["launch_mode"] == "facebook_js_sdk"
    assert start["extras"] == {
        "version": "v4",
        "setup": {},
        "sessionInfoVersion": "3",
        "featureType": "whatsapp_business_app_onboarding",
    }
    assert completed.status_code == 200
    assert completed.json()["data"]["status"] == "completed"
    assert completed.json()["data"]["connection_mode"] == "coexistence"
    assert completed.json()["data"]["credential_present"] is True
    assert replay.status_code == 400
    assert replay.json()["detail"]["code"] == "oauth_state_used"
    assert provider.exchange_calls == 1
    assert "state" not in recovered.text
    assert provider.token not in completed.text + recovered.text
    channel = database_session.scalar(
        select(WhatsAppChannel).where(
            WhatsAppChannel.tenant_id == tenant.id,
            WhatsAppChannel.provider == "meta",
        )
    )
    assert channel is not None
    assert channel.connection_mode == "coexistence"
    assert channel.subscription_status == "subscribed"
    assert decrypt_credential(channel.credential_ciphertext or "") == provider.token
    base_provider = object.__new__(MetaWhatsAppProvider)
    base_provider.access_token = "old-environment-token"
    base_provider.waba_id = "old-environment-waba"
    base_provider.phone_number_id = "old-environment-phone"
    base_provider.graph_api_version = "v26.0"
    selected_provider = provider_for_channel(base_provider, channel)
    assert selected_provider.access_token == provider.token
    assert selected_provider.waba_id == provider.assets.waba_id
    assert selected_provider.phone_number_id == provider.assets.phone_number_id


def test_embedded_signup_requires_update_permission(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, _, _, _, headers = setup(
        client, database_session, role_key=RoleKey.READONLY
    )
    provider = MockEmbeddedSignupProvider()
    try:
        response = _initiate(client, headers, provider)
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)
    assert response.status_code == 403


def test_public_callback_is_safe_registration_landing(client: TestClient) -> None:
    response = client.get(
        "/api/integrations/meta/whatsapp/oauth/callback",
        params={"code": "must-not-be-returned", "state": "must-not-be-returned"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["callback_ready"] is True
    assert "must-not-be-returned" not in response.text


def test_oauth_configuration_diagnostics_cover_missing_and_invalid_settings() -> None:
    required = {
        "META_WHATSAPP_APP_ID": {"meta_whatsapp_app_id": None},
        "META_WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID": {
            "meta_whatsapp_embedded_signup_config_id": None
        },
        "META_WHATSAPP_EMBEDDED_SIGNUP_VERSION": {
            "meta_whatsapp_embedded_signup_version": None
        },
        "META_WHATSAPP_OAUTH_REDIRECT_URI": {
            "meta_whatsapp_oauth_redirect_uri": None
        },
        "META_APP_SECRET": {"meta_app_secret": None},
        "SMTP_CREDENTIAL_ENCRYPTION_KEY": {
            "smtp_credential_encryption_key": None
        },
        "META_GRAPH_API_VERSION": {"meta_graph_api_version": ""},
    }
    for name, override in required.items():
        diagnostic = MetaWhatsAppProvider(
            _settings(**override)
        ).oauth_configuration_diagnostic()
        assert name in diagnostic.missing_settings
        assert diagnostic.ready is False

    invalid_cases = {
        "META_WHATSAPP_APP_ID": {"meta_whatsapp_app_id": "not-an-id"},
        "META_WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID": {
            "meta_whatsapp_embedded_signup_config_id": "not-an-id"
        },
        "META_WHATSAPP_EMBEDDED_SIGNUP_VERSION": {
            "meta_whatsapp_embedded_signup_version": "v6"
        },
        "META_WHATSAPP_OAUTH_REDIRECT_URI": {
            "meta_whatsapp_oauth_redirect_uri": "http://api.example.test/callback"
        },
        "SMTP_CREDENTIAL_ENCRYPTION_KEY": {
            "smtp_credential_encryption_key": "not-a-fernet-key"
        },
        "META_GRAPH_API_VERSION": {"meta_graph_api_version": "latest"},
    }
    for name, override in invalid_cases.items():
        diagnostic = MetaWhatsAppProvider(
            _settings(**override)
        ).oauth_configuration_diagnostic()
        assert name in diagnostic.invalid_settings
        assert diagnostic.ready is False


def test_embedded_signup_version_contracts_are_not_mixed() -> None:
    v4 = MetaWhatsAppProvider(
        _settings(meta_whatsapp_embedded_signup_version=" V4 ")
    )
    assert v4.embedded_signup_options()["extras"] == {
        "version": "v4",
        "setup": {},
        "sessionInfoVersion": "3",
        "featureType": "whatsapp_business_app_onboarding",
    }

    v5 = MetaWhatsAppProvider(
        _settings(meta_whatsapp_embedded_signup_version="v5")
    )
    assert v5.embedded_signup_options()["extras"] == {"version": "v5"}

    unsupported = MetaWhatsAppProvider(
        _settings(meta_whatsapp_embedded_signup_version="v6")
    )
    assert unsupported.oauth_configuration_diagnostic().ready is False
    try:
        unsupported.embedded_signup_options()
    except MetaWhatsAppConfigurationError as exception:
        assert exception.code == "meta_whatsapp_embedded_signup_version_unsupported"
    else:
        raise AssertionError("Unsupported Embedded Signup version was accepted")

    missing = MetaWhatsAppProvider(
        _settings(meta_whatsapp_embedded_signup_version=None)
    )
    assert missing.oauth_configuration_diagnostic().ready is False
    try:
        missing.embedded_signup_options()
    except MetaWhatsAppConfigurationError as exception:
        assert exception.code == "meta_whatsapp_embedded_signup_version_missing"
    else:
        raise AssertionError("Missing Embedded Signup version was accepted")


def test_initiation_rejects_unsupported_embedded_signup_version(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, _, _, _, headers = setup(client, database_session)
    provider = MetaWhatsAppProvider(
        _settings(meta_whatsapp_embedded_signup_version="v6")
    )
    app.dependency_overrides[get_meta_whatsapp_provider] = lambda: provider
    try:
        readiness = client.get(
            "/api/messages/meta/whatsapp/connect/readiness", headers=headers
        )
        response = client.post(
            "/api/messages/meta/whatsapp/connect/initiate", headers=headers
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)

    assert readiness.status_code == 200
    assert readiness.json()["data"]["oauth_configuration_ready"] is False
    assert (
        "META_WHATSAPP_EMBEDDED_SIGNUP_VERSION"
        in readiness.json()["data"]["invalid_settings"]
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "meta_whatsapp_embedded_signup_version_unsupported"
    )
    assert database_session.scalar(select(MetaWhatsAppOAuthOperation)) is None


def test_code_exchange_uses_documented_parameters_without_redirect_uri(
    monkeypatch,
) -> None:
    provider = MetaWhatsAppProvider(_settings())
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "access_token": "synthetic-exchanged-token",
                "token_type": "bearer",
                "expires_in": 3600,
            }

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured["client_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            del args

        async def get(self, url: str, **kwargs):
            captured["url"] = url
            captured["request_options"] = kwargs
            captured["encoded_url"] = str(
                httpx.Request("GET", url, params=kwargs["params"]).url
            )
            return FakeResponse()

    monkeypatch.setattr(
        meta_whatsapp_provider_module.httpx, "AsyncClient", FakeClient
    )
    token = asyncio.run(
        provider.exchange_embedded_signup_code("synthetic-short-lived-code")
    )

    assert token.access_token == "synthetic-exchanged-token"
    assert captured["url"] == "https://graph.facebook.com/v26.0/oauth/access_token"
    assert captured["request_options"] == {
        "params": {
            "client_id": "111111111111111",
            "client_secret": "fixture-app-secret",
            "code": "synthetic-short-lived-code",
        }
    }
    assert "redirect_uri" not in str(captured["encoded_url"])


def test_embedded_signup_expiry_cancellation_and_cross_tenant_conflict(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    provider = MockEmbeddedSignupProvider()
    try:
        started = _initiate(client, headers, provider).json()["data"]
        operation = database_session.get(
            MetaWhatsAppOAuthOperation, uuid.UUID(started["operation_id"])
        )
        operation.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        database_session.commit()
        expired = client.post(
            "/api/messages/meta/whatsapp/connect/complete",
            headers=headers,
            json=_complete_payload(started, provider),
        )

        cancelled_start = _initiate(client, headers, provider).json()["data"]
        cancelled = client.post(
            "/api/messages/meta/whatsapp/connect/cancel",
            headers=headers,
            json={
                "operation_id": cancelled_start["operation_id"],
                "state": cancelled_start["state"],
            },
        )

        conflict_start = _initiate(client, headers, provider).json()["data"]
        other = Tenant(name="Other OAuth Tenant", slug="other-oauth-tenant")
        database_session.add(other)
        database_session.flush()
        database_session.add(
            WhatsAppChannel(
                tenant_id=other.id,
                provider="meta",
                provider_account_id=provider.assets.waba_id,
                provider_channel_id=provider.assets.phone_number_id,
                sender_address=provider.assets.sender_address,
                is_active=True,
            )
        )
        database_session.commit()
        conflict = client.post(
            "/api/messages/meta/whatsapp/connect/complete",
            headers=headers,
            json=_complete_payload(conflict_start, provider),
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)

    assert expired.status_code == 400
    assert expired.json()["detail"]["code"] == "oauth_state_expired"
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert conflict.status_code == 409
    assert database_session.scalar(
        select(WhatsAppChannel).where(
            WhatsAppChannel.tenant_id == tenant.id,
            WhatsAppChannel.provider == "meta",
        )
    ) is None


def test_existing_sender_confirmation_and_subscription_recovery(
    client: TestClient,
    database_session: Session,
) -> None:
    tenant, _, _, _, _, _, headers = setup(client, database_session)
    old = WhatsAppChannel(
        tenant_id=tenant.id,
        provider="meta",
        provider_account_id="666666666666666",
        provider_channel_id="777777777777777",
        sender_address="+14155550999",
        is_active=True,
    )
    database_session.add(old)
    database_session.commit()
    provider = MockEmbeddedSignupProvider()
    try:
        started = _initiate(client, headers, provider).json()["data"]
        completed = client.post(
            "/api/messages/meta/whatsapp/connect/complete",
            headers=headers,
            json=_complete_payload(started, provider),
        )
        denied = client.post(
            f"/api/messages/meta/whatsapp/connect/operations/{started['operation_id']}/activate",
            headers=headers,
            json={"confirm_replace_existing_sender": False},
        )
        activated = client.post(
            f"/api/messages/meta/whatsapp/connect/operations/{started['operation_id']}/activate",
            headers=headers,
            json={"confirm_replace_existing_sender": True},
        )

        provider.subscription_failure = MetaWhatsAppOAuthError(
            code="fixture_subscription_failure",
            safe_message="Sanitized subscription failure",
            http_status=400,
        )
        failure_start = _initiate(client, headers, provider).json()["data"]
        failure = client.post(
            "/api/messages/meta/whatsapp/connect/complete",
            headers=headers,
            json=_complete_payload(failure_start, provider),
        )
        provider.subscription_failure = None
        recovered = client.post(
            f"/api/messages/meta/whatsapp/connect/operations/{failure_start['operation_id']}/activate",
            headers=headers,
            json={"confirm_replace_existing_sender": False},
        )
    finally:
        app.dependency_overrides.pop(get_meta_whatsapp_provider, None)

    assert completed.json()["data"]["status"] == "confirmation_required"
    assert denied.status_code == 400
    assert activated.json()["data"]["status"] == "completed"
    database_session.refresh(old)
    assert old.is_active is False
    assert failure.json()["data"]["status"] == "subscription_failed"
    assert failure.json()["data"]["reason_code"] == "fixture_subscription_failure"
    assert recovered.json()["data"]["status"] == "completed"
