from collections.abc import Generator
from contextlib import contextmanager
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import PermissionKey, ROLE_PERMISSIONS, RoleKey
from app.main import app
from app.models.membership import TenantMembership
from app.models.role import Permission, Role, RolePermission
from app.models.sender_account import SenderAccount
from app.models.tenant import Tenant
from app.models.user import User
from app.providers.smtp import (
    SmtpAuthenticationError,
    SmtpDeliveryConfig,
    SmtpProvider,
    get_smtp_provider,
)
from app.utils.credential_encryption import decrypt_credential
from app.utils.password import hash_password

PASSWORD = "12345678"


class MockSmtpProvider(SmtpProvider):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.connection_configs: list[SmtpDeliveryConfig] = []
        self.sent: list[tuple[SmtpDeliveryConfig, str]] = []

    async def test_connection(self, config: SmtpDeliveryConfig) -> None:
        self.connection_configs.append(config)
        if self.fail:
            raise SmtpAuthenticationError

    async def send_test_email(
        self,
        config: SmtpDeliveryConfig,
        recipient: str,
    ) -> None:
        self.sent.append((config, recipient))
        if self.fail:
            raise SmtpAuthenticationError


def seed_roles(database_session: Session) -> dict[RoleKey, Role]:
    roles = {
        key: Role(key=key.value, name=key.value.title(), is_system=True)
        for key in RoleKey
    }
    permissions = {
        key.value: Permission(key=key.value, description=key.value)
        for key in PermissionKey
    }
    database_session.add_all([*roles.values(), *permissions.values()])
    database_session.flush()
    database_session.add_all(
        RolePermission(
            role_id=roles[role_key].id,
            permission_id=permissions[permission.value].id,
        )
        for role_key, role_permissions in ROLE_PERMISSIONS.items()
        for permission in role_permissions
    )
    database_session.commit()
    return roles


def make_workspace_user(
    database_session: Session,
    role: Role,
    suffix: str,
    *,
    tenant: Tenant | None = None,
) -> tuple[Tenant, User]:
    tenant = tenant or Tenant(name=f"Tenant {suffix}", slug=f"tenant-{suffix}")
    user = User(
        name=f"User {suffix}",
        email=f"user.{suffix}@example.com",
        login_id=f"user.{suffix}",
        password_hash=hash_password(PASSWORD),
    )
    database_session.add_all([tenant, user])
    database_session.flush()
    database_session.add(
        TenantMembership(tenant_id=tenant.id, user_id=user.id, role_id=role.id)
    )
    database_session.commit()
    return tenant, user


def login(client: TestClient, user: User) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"identifier": user.login_id, "password": PASSWORD},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def payload(email: str, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "display_name": "Sales Team",
        "sender_email": email,
        "provider": "custom_smtp",
        "authentication_type": "password",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "encryption": "STARTTLS",
        "smtp_username": email,
        "credential": "smtp-secret-value",
        "reply_to": "replies@example.com",
        "is_active": True,
        "is_default": False,
    }
    result.update(overrides)
    return result


def owner_setup(
    client: TestClient,
    database_session: Session,
) -> tuple[dict[RoleKey, Role], Tenant, User, dict[str, str]]:
    roles = seed_roles(database_session)
    tenant, owner = make_workspace_user(database_session, roles[RoleKey.OWNER], "owner")
    return roles, tenant, owner, login(client, owner)


@contextmanager
def override_provider(provider: MockSmtpProvider) -> Generator[None, None, None]:
    app.dependency_overrides[get_smtp_provider] = lambda: provider
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_smtp_provider, None)


def test_create_encrypts_credentials_and_never_returns_them(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = owner_setup(client, database_session)
    response = client.post(
        "/api/mail/sender-accounts",
        headers=auth,
        json=payload("sales@example.com"),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["data"]["credentials_configured"] is True
    assert "credential" not in body["data"]
    assert "credential_ciphertext" not in body["data"]

    account = database_session.scalar(select(SenderAccount))
    assert account is not None
    assert account.credential_ciphertext != "smtp-secret-value"
    assert decrypt_credential(account.credential_ciphertext) == "smtp-secret-value"


def test_omitted_credential_is_preserved_and_new_credential_replaces_it(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = owner_setup(client, database_session)
    created = client.post(
        "/api/mail/sender-accounts",
        headers=auth,
        json=payload("preserve@example.com"),
    )
    account_id = uuid.UUID(created.json()["data"]["id"])
    before = database_session.get(SenderAccount, account_id).credential_ciphertext

    updated = client.patch(
        f"/api/mail/sender-accounts/{account_id}",
        headers=auth,
        json={"display_name": "Updated Sender"},
    )
    assert updated.status_code == 200
    database_session.expire_all()
    assert database_session.get(SenderAccount, account_id).credential_ciphertext == before

    replaced = client.patch(
        f"/api/mail/sender-accounts/{account_id}",
        headers=auth,
        json={"credential": "replacement-secret"},
    )
    assert replaced.status_code == 200
    database_session.expire_all()
    after = database_session.get(SenderAccount, account_id).credential_ciphertext
    assert after != before
    assert decrypt_credential(after) == "replacement-secret"


def test_default_is_unique_and_deactivation_clears_default(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = owner_setup(client, database_session)
    first = client.post(
        "/api/mail/sender-accounts",
        headers=auth,
        json=payload("first@example.com", is_default=True),
    ).json()["data"]
    second = client.post(
        "/api/mail/sender-accounts",
        headers=auth,
        json=payload("second@example.com", is_default=True),
    ).json()["data"]
    database_session.expire_all()
    accounts = database_session.scalars(select(SenderAccount)).all()
    assert sum(account.is_default for account in accounts) == 1
    assert str(next(account.id for account in accounts if account.is_default)) == second["id"]

    response = client.post(
        f"/api/mail/sender-accounts/{second['id']}/deactivate",
        headers=auth,
    )
    assert response.status_code == 200
    assert response.json()["data"]["is_default"] is False
    assert first["id"] != second["id"]


def test_permissions_and_tenant_isolation(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, _, _, owner_auth = owner_setup(client, database_session)
    created = client.post(
        "/api/mail/sender-accounts",
        headers=owner_auth,
        json=payload("private@example.com"),
    )
    account_id = created.json()["data"]["id"]

    other_tenant, other_owner = make_workspace_user(
        database_session,
        roles[RoleKey.OWNER],
        "other",
    )
    del other_tenant
    other_auth = login(client, other_owner)
    assert client.get(
        f"/api/mail/sender-accounts/{account_id}", headers=other_auth
    ).status_code == 404

    _, readonly = make_workspace_user(
        database_session,
        roles[RoleKey.READONLY],
        "readonly",
    )
    readonly_auth = login(client, readonly)
    assert client.get("/api/mail/sender-accounts", headers=readonly_auth).status_code == 403
    assert client.post(
        "/api/mail/sender-accounts",
        headers=readonly_auth,
        json=payload("blocked@example.com"),
    ).status_code == 403


def test_connection_and_test_email_use_mock_provider(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = owner_setup(client, database_session)
    created = client.post(
        "/api/mail/sender-accounts",
        headers=auth,
        json=payload("mocked@example.com"),
    )
    account_id = created.json()["data"]["id"]
    provider = MockSmtpProvider()
    with override_provider(provider):
        tested = client.post(
            f"/api/mail/sender-accounts/{account_id}/test-connection",
            headers=auth,
        )
        assert tested.status_code == 200
        assert tested.json()["data"]["connection_status"] == "success"
        assert len(provider.connection_configs) == 1

        sent = client.post(
            f"/api/mail/sender-accounts/{account_id}/test-email",
            headers=auth,
            json={"recipient": "recipient@example.com"},
        )
        assert sent.status_code == 200
        assert len(provider.sent) == 1


def test_smtp_failure_is_sanitized_and_recorded(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = owner_setup(client, database_session)
    created = client.post(
        "/api/mail/sender-accounts",
        headers=auth,
        json=payload("failure@example.com"),
    )
    account_id = created.json()["data"]["id"]
    provider = MockSmtpProvider(fail=True)
    with override_provider(provider):
        response = client.post(
            f"/api/mail/sender-accounts/{account_id}/test-connection",
            headers=auth,
        )
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["message"] == "SMTP authentication failed"
    database_session.expire_all()
    account = database_session.get(SenderAccount, uuid.UUID(account_id))
    assert account.connection_status == "failed"
    assert account.last_tested_at is not None


def test_duplicate_email_gmail_defaults_and_insecure_host_validation(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, auth = owner_setup(client, database_session)
    gmail = {
        "display_name": "Gmail Sender",
        "sender_email": "gmail@example.com",
        "provider": "gmail",
        "authentication_type": "app_password",
        "credential": "google-app-password",
    }
    first = client.post("/api/mail/sender-accounts", headers=auth, json=gmail)
    assert first.status_code == 201
    assert first.json()["data"]["smtp_host"] == "smtp.gmail.com"
    duplicate = client.post("/api/mail/sender-accounts", headers=auth, json=gmail)
    assert duplicate.status_code == 409

    blocked = client.post(
        "/api/mail/sender-accounts",
        headers=auth,
        json=payload("blocked@example.com", smtp_host="127.0.0.1"),
    )
    assert blocked.status_code == 422
