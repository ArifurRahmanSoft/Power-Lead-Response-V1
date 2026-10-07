from datetime import datetime, timezone
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import RoleKey
from app.commands import bootstrap_workspace as bootstrap_command
from app.models.authorization_audit import AuthorizationAudit
from app.models.membership import TenantMembership
from app.models.role import Permission, Role, RolePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.services.bootstrap_service import (
    REQUIRED_OWNER_SETTINGS_PERMISSIONS,
    BootstrapUserNotFoundError,
    bootstrap_owner_workspace,
)
from app.utils.password import hash_password


def test_bootstrap_command_is_disabled_in_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap_command.settings, "live", True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap_workspace",
            "--identifier",
            "existing.user",
            "--workspace-name",
            "Local Workspace",
        ],
    )

    with pytest.raises(SystemExit, match="disabled when LIVE=true"):
        bootstrap_command.main()


def seed_bootstrap_prerequisites(database_session: Session) -> tuple[User, Role]:
    user = User(
        name="Existing Account",
        email="existing@example.com",
        login_id="existing.user",
        password_hash=hash_password("existing-password"),
        is_active=True,
    )
    owner_role = Role(
        key=RoleKey.OWNER.value,
        name="Owner",
        tenant_id=None,
        is_system=True,
    )
    database_session.add_all([user, owner_role])
    for key in sorted(REQUIRED_OWNER_SETTINGS_PERMISSIONS):
        database_session.add(
            Permission(
                key=key,
                description=key.replace(".", " ").replace("_", " ").title(),
            )
        )
    database_session.commit()
    return user, owner_role


def test_bootstrap_is_transactional_idempotent_and_repairs_owner_permissions(
    database_session: Session,
) -> None:
    user, owner_role = seed_bootstrap_prerequisites(database_session)

    first = bootstrap_owner_workspace(
        database_session,
        identifier=user.email.upper(),
        workspace_name="  PowerLead   Local  ",
    )
    database_session.commit()

    assert first.created_tenant is True
    assert first.created_membership is True
    assert first.tenant.slug == "powerlead-local"
    assert first.membership.role_id == owner_role.id
    assert first.membership.is_active is True
    assert first.membership.revoked_at is None
    assert set(first.repaired_permission_keys) == REQUIRED_OWNER_SETTINGS_PERMISSIONS

    second = bootstrap_owner_workspace(
        database_session,
        identifier=user.login_id,
        workspace_name="PowerLead Local",
    )
    database_session.commit()

    assert second.created_tenant is False
    assert second.created_membership is False
    assert second.tenant.id == first.tenant.id
    assert second.membership.id == first.membership.id
    assert second.repaired_permission_keys == ()
    assert database_session.scalar(select(func.count(Tenant.id))) == 1
    assert database_session.scalar(select(func.count(TenantMembership.id))) == 1
    assert database_session.scalar(select(func.count(AuthorizationAudit.id))) == 1
    assert (
        database_session.scalar(
            select(func.count(RolePermission.permission_id)).where(
                RolePermission.role_id == owner_role.id
            )
        )
        == len(REQUIRED_OWNER_SETTINGS_PERMISSIONS)
    )


def test_bootstrap_reuses_workspace_and_reactivates_membership(
    database_session: Session,
) -> None:
    user, owner_role = seed_bootstrap_prerequisites(database_session)
    readonly_role = Role(
        key=RoleKey.READONLY.value,
        name="Readonly",
        tenant_id=None,
        is_system=True,
    )
    tenant = Tenant(name="Old Name", slug="local-workspace", is_active=False)
    database_session.add_all([readonly_role, tenant])
    database_session.flush()
    membership = TenantMembership(
        tenant_id=tenant.id,
        user_id=user.id,
        role_id=readonly_role.id,
        is_active=False,
        revoked_at=datetime.now(timezone.utc),
    )
    database_session.add(membership)
    database_session.commit()

    result = bootstrap_owner_workspace(
        database_session,
        identifier=user.login_id,
        workspace_name="Local Workspace",
    )
    database_session.commit()

    assert result.tenant.id == tenant.id
    assert result.tenant.name == "Local Workspace"
    assert result.tenant.is_active is True
    assert result.membership.id == membership.id
    assert result.membership.role_id == owner_role.id
    assert result.membership.is_active is True
    assert result.membership.revoked_at is None


def test_bootstrap_never_matches_display_name(database_session: Session) -> None:
    seed_bootstrap_prerequisites(database_session)

    with pytest.raises(BootstrapUserNotFoundError):
        bootstrap_owner_workspace(
            database_session,
            identifier="Existing Account",
            workspace_name="Local Workspace",
        )
    database_session.rollback()


def test_fresh_login_and_me_return_owner_workspace_and_settings_permissions(
    client: TestClient,
    database_session: Session,
) -> None:
    user, _ = seed_bootstrap_prerequisites(database_session)
    result = bootstrap_owner_workspace(
        database_session,
        identifier=user.login_id,
        workspace_name="Local Workspace",
    )
    database_session.commit()

    login_response = client.post(
        "/api/auth/login",
        json={
            "identifier": user.login_id,
            "password": "existing-password",
        },
    )
    assert login_response.status_code == 200
    login_data = login_response.json()["data"]
    assert login_data["auth_status"] == "workspace_selected"
    assert login_data["selected_workspace"]["id"] == str(result.tenant.id)
    assert login_data["role"] == "owner"
    assert REQUIRED_OWNER_SETTINGS_PERMISSIONS <= set(login_data["permissions"])

    me_response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {login_data['access_token']}"},
    )
    assert me_response.status_code == 200
    me_data = me_response.json()["data"]
    assert me_data["selected_workspace"]["id"] == str(result.tenant.id)
    assert me_data["role"] == "owner"
    assert REQUIRED_OWNER_SETTINGS_PERMISSIONS <= set(me_data["permissions"])
