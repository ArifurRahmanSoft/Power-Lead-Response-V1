from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import PermissionKey, ROLE_PERMISSIONS, RoleKey
from app.core.security import WorkspaceContext, enforce_lead_scope
from app.models.authorization_audit import AuthorizationAudit
from app.models.membership import TenantMembership
from app.models.role import Permission, Role, RolePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.utils.password import hash_password

PASSWORD = "12345678"


def seed_roles_and_permissions(database_session: Session) -> dict[RoleKey, Role]:
    roles = {
        role_key: Role(key=role_key.value, name=role_key.value.title())
        for role_key in RoleKey
    }
    permissions = {
        permission_key: Permission(
            key=permission_key.value,
            description=permission_key.value,
        )
        for permission_key in PermissionKey
    }
    database_session.add_all([*roles.values(), *permissions.values()])
    database_session.flush()
    database_session.add_all(
        RolePermission(
            role_id=roles[role_key].id,
            permission_id=permissions[permission_key].id,
        )
        for role_key, permission_keys in ROLE_PERMISSIONS.items()
        for permission_key in permission_keys
    )
    database_session.commit()
    return roles


def create_user(database_session: Session, login_id: str) -> User:
    user = User(
        name=login_id.replace(".", " ").title(),
        login_id=login_id,
        email=f"{login_id}@example.com",
        password_hash=hash_password(PASSWORD),
    )
    database_session.add(user)
    database_session.commit()
    database_session.refresh(user)
    return user


def create_workspace(database_session: Session, slug: str) -> Tenant:
    tenant = Tenant(name=slug.replace("-", " ").title(), slug=slug)
    database_session.add(tenant)
    database_session.commit()
    database_session.refresh(tenant)
    return tenant


def add_membership(
    database_session: Session,
    user: User,
    tenant: Tenant,
    role: Role,
) -> TenantMembership:
    membership = TenantMembership(
        user_id=user.id,
        tenant_id=tenant.id,
        role_id=role.id,
    )
    database_session.add(membership)
    database_session.commit()
    database_session.refresh(membership)
    return membership


def login(client: TestClient, identifier: str) -> dict[str, object]:
    response = client.post(
        "/api/auth/login",
        json={"identifier": identifier, "password": PASSWORD},
    )
    assert response.status_code == 200
    return response.json()["data"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_single_workspace_resolves_role_and_effective_permissions(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_roles_and_permissions(database_session)
    user = create_user(database_session, "single.user")
    tenant = create_workspace(database_session, "single-workspace")
    add_membership(database_session, user, tenant, roles[RoleKey.STAFF])

    login_data = login(client, user.login_id)

    assert login_data["auth_status"] == "workspace_selected"
    assert login_data["selected_workspace"] == {
        "id": str(tenant.id),
        "name": tenant.name,
        "slug": tenant.slug,
    }
    assert login_data["role"] == RoleKey.STAFF.value
    assert set(login_data["permissions"]) == {
        permission.value for permission in ROLE_PERMISSIONS[RoleKey.STAFF]
    }

    me_response = client.get(
        "/api/auth/me",
        headers=bearer(str(login_data["access_token"])),
    )
    assert me_response.status_code == 200
    assert me_response.json()["data"]["role"] == RoleKey.STAFF.value
    assert set(me_response.json()["data"]["permissions"]) == set(
        login_data["permissions"]
    )


def test_multiple_workspaces_require_selection(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_roles_and_permissions(database_session)
    user = create_user(database_session, "multi.user")
    first = create_workspace(database_session, "first-workspace")
    second = create_workspace(database_session, "second-workspace")
    unauthorized = create_workspace(database_session, "unauthorized-workspace")
    add_membership(database_session, user, first, roles[RoleKey.STAFF])
    add_membership(database_session, user, second, roles[RoleKey.READONLY])

    login_data = login(client, user.login_id)

    assert login_data["auth_status"] == "workspace_selection_required"
    assert login_data["selected_workspace"] is None
    assert login_data["permissions"] == []
    assert len(login_data["workspaces"]) == 2

    unauthorized_selection = client.post(
        "/api/auth/select-workspace",
        headers=bearer(str(login_data["access_token"])),
        json={"workspace_id": str(unauthorized.id)},
    )
    assert unauthorized_selection.status_code == 403

    selection_response = client.post(
        "/api/auth/select-workspace",
        headers=bearer(str(login_data["access_token"])),
        json={"workspace_id": str(first.id)},
    )
    assert selection_response.status_code == 200
    selected_data = selection_response.json()["data"]
    assert selected_data["auth_status"] == "workspace_selected"
    assert selected_data["selected_workspace"]["id"] == str(first.id)
    assert selected_data["role"] == "staff"


def test_operator_cannot_manage_roles(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_roles_and_permissions(database_session)
    operator = create_user(database_session, "operator.user")
    target = create_user(database_session, "target.user")
    tenant = create_workspace(database_session, "operator-workspace")
    add_membership(database_session, operator, tenant, roles[RoleKey.OPERATOR])
    target_membership = add_membership(
        database_session,
        target,
        tenant,
        roles[RoleKey.STAFF],
    )
    login_data = login(client, operator.login_id)

    response = client.patch(
        f"/api/workspaces/{tenant.id}/members/{target_membership.id}/role",
        headers=bearer(str(login_data["access_token"])),
        json={"role": "readonly"},
    )
    assert response.status_code == 403


def test_revoked_membership_is_rejected_immediately(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_roles_and_permissions(database_session)
    user = create_user(database_session, "revoked.user")
    tenant = create_workspace(database_session, "revoked-workspace")
    membership = add_membership(database_session, user, tenant, roles[RoleKey.STAFF])
    login_data = login(client, user.login_id)
    membership.is_active = False
    membership.revoked_at = datetime.now(timezone.utc)
    database_session.commit()

    response = client.get(
        "/api/auth/me",
        headers=bearer(str(login_data["access_token"])),
    )
    assert response.status_code == 403


def test_cross_tenant_access_is_forbidden(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_roles_and_permissions(database_session)
    owner = create_user(database_session, "owner.user")
    owned_tenant = create_workspace(database_session, "owned-workspace")
    other_tenant = create_workspace(database_session, "other-workspace")
    add_membership(database_session, owner, owned_tenant, roles[RoleKey.OWNER])
    login_data = login(client, owner.login_id)

    response = client.get(
        f"/api/workspaces/{other_tenant.id}/members",
        headers=bearer(str(login_data["access_token"])),
    )
    assert response.status_code == 403


def test_role_changes_take_effect_for_existing_tokens(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_roles_and_permissions(database_session)
    owner = create_user(database_session, "primary.owner")
    operator = create_user(database_session, "changing.operator")
    tenant = create_workspace(database_session, "role-change-workspace")
    add_membership(database_session, owner, tenant, roles[RoleKey.OWNER])
    operator_membership = add_membership(
        database_session,
        operator,
        tenant,
        roles[RoleKey.OPERATOR],
    )
    owner_login = login(client, owner.login_id)
    operator_login = login(client, operator.login_id)

    change_response = client.patch(
        f"/api/workspaces/{tenant.id}/members/{operator_membership.id}/role",
        headers=bearer(str(owner_login["access_token"])),
        json={"role": "readonly"},
    )
    assert change_response.status_code == 200
    me_response = client.get(
        "/api/auth/me",
        headers=bearer(str(operator_login["access_token"])),
    )
    assert me_response.status_code == 200
    assert me_response.json()["data"]["role"] == "readonly"
    assert database_session.scalar(select(AuthorizationAudit)) is not None


def test_last_active_owner_cannot_be_demoted(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_roles_and_permissions(database_session)
    owner = create_user(database_session, "only.owner")
    tenant = create_workspace(database_session, "single-owner-workspace")
    membership = add_membership(database_session, owner, tenant, roles[RoleKey.OWNER])
    login_data = login(client, owner.login_id)

    response = client.patch(
        f"/api/workspaces/{tenant.id}/members/{membership.id}/role",
        headers=bearer(str(login_data["access_token"])),
        json={"role": "operator"},
    )
    assert response.status_code == 409


def test_operator_integration_access_requires_explicit_delegation(
    client: TestClient,
    database_session: Session,
) -> None:
    roles = seed_roles_and_permissions(database_session)
    owner = create_user(database_session, "integration.owner")
    operator = create_user(database_session, "integration.operator")
    tenant = create_workspace(database_session, "integration-workspace")
    add_membership(database_session, owner, tenant, roles[RoleKey.OWNER])
    operator_membership = add_membership(
        database_session,
        operator,
        tenant,
        roles[RoleKey.OPERATOR],
    )
    owner_login = login(client, owner.login_id)
    operator_login = login(client, operator.login_id)
    assert PermissionKey.INTEGRATIONS_USE.value not in operator_login["permissions"]

    delegation_response = client.post(
        f"/api/workspaces/{tenant.id}/members/"
        f"{operator_membership.id}/integration-access",
        headers=bearer(str(owner_login["access_token"])),
    )
    assert delegation_response.status_code == 200

    me_response = client.get(
        "/api/auth/me",
        headers=bearer(str(operator_login["access_token"])),
    )
    assert me_response.status_code == 200
    assert PermissionKey.INTEGRATIONS_USE.value in me_response.json()["data"][
        "permissions"
    ]

    role_change_response = client.patch(
        f"/api/workspaces/{tenant.id}/members/{operator_membership.id}/role",
        headers=bearer(str(owner_login["access_token"])),
        json={"role": "readonly"},
    )
    assert role_change_response.status_code == 200
    updated_me_response = client.get(
        "/api/auth/me",
        headers=bearer(str(operator_login["access_token"])),
    )
    assert PermissionKey.INTEGRATIONS_USE.value not in updated_me_response.json()[
        "data"
    ]["permissions"]


def test_staff_write_scope_requires_assignment(database_session: Session) -> None:
    roles = seed_roles_and_permissions(database_session)
    staff = create_user(database_session, "assigned.staff")
    tenant = create_workspace(database_session, "lead-scope-workspace")
    membership = add_membership(database_session, staff, tenant, roles[RoleKey.STAFF])
    context = WorkspaceContext(
        user=staff,
        tenant=tenant,
        membership=membership,
        role=RoleKey.STAFF.value,
        permissions=frozenset(key.value for key in ROLE_PERMISSIONS[RoleKey.STAFF]),
    )

    enforce_lead_scope(
        context,
        lead_tenant_id=tenant.id,
        assigned_user_id=staff.id,
        write=True,
    )
    with pytest.raises(HTTPException):
        enforce_lead_scope(
            context,
            lead_tenant_id=tenant.id,
            assigned_user_id=None,
            write=True,
        )
