import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import PermissionKey, ROLE_PERMISSIONS, RoleKey
from app.models.membership import TenantMembership
from app.models.role import Permission, Role, RolePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.utils.password import hash_password, verify_password

PASSWORD = "12345678"


def seed_authorization(database_session: Session) -> tuple[dict[RoleKey, Role], dict[str, Permission]]:
    roles = {
        key: Role(
            key=key.value,
            name=key.value.title(),
            is_system=True,
        )
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
            permission_id=permissions[permission_key.value].id,
        )
        for role_key, permission_keys in ROLE_PERMISSIONS.items()
        for permission_key in permission_keys
    )
    database_session.commit()
    return roles, permissions


def create_tenant(database_session: Session, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    database_session.add(tenant)
    database_session.commit()
    database_session.refresh(tenant)
    return tenant


def create_user(database_session: Session, login_id: str) -> User:
    user = User(
        name=login_id.title(),
        login_id=login_id,
        email=f"{login_id}@example.com",
        password_hash=hash_password(PASSWORD),
    )
    database_session.add(user)
    database_session.commit()
    database_session.refresh(user)
    return user


def add_membership(
    database_session: Session,
    tenant: Tenant,
    user: User,
    role: Role,
) -> TenantMembership:
    membership = TenantMembership(
        tenant_id=tenant.id,
        user_id=user.id,
        role_id=role.id,
    )
    database_session.add(membership)
    database_session.commit()
    database_session.refresh(membership)
    return membership


def login(client: TestClient, login_id: str) -> tuple[str, dict[str, object]]:
    response = client.post(
        "/api/auth/login",
        json={"identifier": login_id, "password": PASSWORD},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    return str(data["access_token"]), data


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def owner_setup(
    client: TestClient,
    database_session: Session,
) -> tuple[dict[RoleKey, Role], Tenant, User, str]:
    roles, _ = seed_authorization(database_session)
    tenant = create_tenant(database_session, "settings-tenant")
    owner = create_user(database_session, "settings.owner")
    add_membership(database_session, tenant, owner, roles[RoleKey.OWNER])
    token, _ = login(client, owner.login_id)
    return roles, tenant, owner, token


def test_role_crud_and_case_insensitive_duplicate(
    client: TestClient,
    database_session: Session,
) -> None:
    _, _, _, token = owner_setup(client, database_session)
    created = client.post(
        "/api/settings/roles",
        headers=headers(token),
        json={"name": "Sales Manager"},
    )
    assert created.status_code == 201
    role_id = created.json()["data"]["id"]
    assert created.json()["data"]["is_system"] is False

    duplicate = client.post(
        "/api/settings/roles",
        headers=headers(token),
        json={"name": "  SALES   MANAGER  "},
    )
    assert duplicate.status_code == 409

    updated = client.patch(
        f"/api/settings/roles/{role_id}",
        headers=headers(token),
        json={"name": "Sales Lead"},
    )
    assert updated.status_code == 200
    listed = client.get("/api/settings/roles?page=1&size=10", headers=headers(token))
    assert listed.status_code == 200
    assert any(item["name"] == "Sales Lead" for item in listed.json()["data"]["items"])
    deleted = client.delete(f"/api/settings/roles/{role_id}", headers=headers(token))
    assert deleted.status_code == 200


def test_system_role_is_immutable_and_assigned_role_cannot_be_deleted(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, tenant, _, token = owner_setup(client, database_session)
    rename = client.patch(
        f"/api/settings/roles/{roles[RoleKey.OWNER].id}",
        headers=headers(token),
        json={"name": "Super Owner"},
    )
    assert rename.status_code == 409

    role_response = client.post(
        "/api/settings/roles",
        headers=headers(token),
        json={"name": "Assigned Custom"},
    )
    role = database_session.get(Role, uuid.UUID(role_response.json()["data"]["id"]))
    member = create_user(database_session, "assigned.member")
    add_membership(database_session, tenant, member, role)
    delete_response = client.delete(
        f"/api/settings/roles/{role.id}",
        headers=headers(token),
    )
    assert delete_response.status_code == 409


def test_menu_permissions_validate_combinations_and_revoke_live_access(
    client: TestClient,
    database_session: Session,
) -> None:
    _, tenant, _, owner_token = owner_setup(client, database_session)
    role_response = client.post(
        "/api/settings/roles",
        headers=headers(owner_token),
        json={"name": "Lead Editor"},
    )
    role_id = role_response.json()["data"]["id"]
    invalid = client.put(
        f"/api/settings/roles/{role_id}/permissions",
        headers=headers(owner_token),
        json={"permission_keys": ["leads.update"]},
    )
    assert invalid.status_code == 400

    granted = client.put(
        f"/api/settings/roles/{role_id}/permissions",
        headers=headers(owner_token),
        json={"permission_keys": ["leads.view", "leads.update"]},
    )
    assert granted.status_code == 200
    role = database_session.get(Role, uuid.UUID(role_id))
    staff = create_user(database_session, "lead.editor")
    add_membership(database_session, tenant, staff, role)
    staff_token, login_data = login(client, staff.login_id)
    assert "leads.update" in login_data["permissions"]

    revoked = client.put(
        f"/api/settings/roles/{role_id}/permissions",
        headers=headers(owner_token),
        json={"permission_keys": ["leads.view"]},
    )
    assert revoked.status_code == 200
    me = client.get("/api/auth/me", headers=headers(staff_token))
    assert "leads.update" not in me.json()["data"]["permissions"]


def test_delegated_manager_cannot_escalate_privileges(
    client: TestClient,
    database_session: Session,
) -> None:
    _, tenant, _, owner_token = owner_setup(client, database_session)
    manager_role_response = client.post(
        "/api/settings/roles",
        headers=headers(owner_token),
        json={"name": "Permission Manager"},
    )
    manager_role_id = manager_role_response.json()["data"]["id"]
    client.put(
        f"/api/settings/roles/{manager_role_id}/permissions",
        headers=headers(owner_token),
        json={
            "permission_keys": [
                "settings.menu_permissions.view",
                "settings.menu_permissions.update",
            ]
        },
    )
    target_role_response = client.post(
        "/api/settings/roles",
        headers=headers(owner_token),
        json={"name": "Target Role"},
    )
    manager = create_user(database_session, "permission.manager")
    manager_role = database_session.get(Role, uuid.UUID(manager_role_id))
    add_membership(database_session, tenant, manager, manager_role)
    manager_token, _ = login(client, manager.login_id)

    escalation = client.put(
        f"/api/settings/roles/{target_role_response.json()['data']['id']}/permissions",
        headers=headers(manager_token),
        json={
            "permission_keys": [
                "settings.users.view",
                "settings.users.create",
            ]
        },
    )
    assert escalation.status_code == 403


def test_user_setup_crud_password_security_and_duplicate_login_id(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, _, _, token = owner_setup(client, database_session)
    request = {
        "display_name": "Setup User",
        "email": "setup.user@example.com",
        "login_id": "setup.user",
        "password": "initial-password",
        "confirm_password": "initial-password",
        "role_id": str(roles[RoleKey.STAFF].id),
    }
    created = client.post("/api/settings/users", headers=headers(token), json=request)
    assert created.status_code == 201
    user_id = created.json()["data"]["id"]
    assert "password" not in created.text.lower()
    stored_user = database_session.get(User, uuid.UUID(user_id))
    assert verify_password("initial-password", stored_user.password_hash)

    updated = client.patch(
        f"/api/settings/users/{user_id}",
        headers=headers(token),
        json={
            "display_name": "Updated Setup User",
            "role_id": str(roles[RoleKey.READONLY].id),
        },
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["display_name"] == "Updated Setup User"
    assert updated.json()["data"]["role_name"] == "Readonly"

    duplicate = client.post(
        "/api/settings/users",
        headers=headers(token),
        json={**request, "email": "other@example.com", "login_id": "SETUP.USER"},
    )
    assert duplicate.status_code == 409

    reset = client.post(
        f"/api/settings/users/{user_id}/reset-password",
        headers=headers(token),
        json={"password": "new-password", "confirm_password": "new-password"},
    )
    assert reset.status_code == 200
    database_session.expire_all()
    assert verify_password(
        "new-password",
        database_session.get(User, uuid.UUID(user_id)).password_hash,
    )
    assert "new-password" not in reset.text

    revoked = client.delete(f"/api/settings/users/{user_id}", headers=headers(token))
    assert revoked.status_code == 200


def test_multi_tenant_global_account_changes_require_separate_flow(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, tenant, _, token = owner_setup(client, database_session)
    target = create_user(database_session, "multi.account")
    add_membership(database_session, tenant, target, roles[RoleKey.STAFF])
    second_tenant = create_tenant(database_session, "second-account-tenant")
    add_membership(database_session, second_tenant, target, roles[RoleKey.READONLY])

    name_update = client.patch(
        f"/api/settings/users/{target.id}",
        headers=headers(token),
        json={"display_name": "Changed Globally"},
    )
    password_reset = client.post(
        f"/api/settings/users/{target.id}/reset-password",
        headers=headers(token),
        json={"password": "new-password", "confirm_password": "new-password"},
    )

    assert name_update.status_code == 409
    assert password_reset.status_code == 409


def test_last_owner_and_cross_tenant_role_are_protected(
    client: TestClient,
    database_session: Session,
) -> None:
    roles, _, owner, token = owner_setup(client, database_session)
    delete_owner = client.delete(
        f"/api/settings/users/{owner.id}",
        headers=headers(token),
    )
    assert delete_owner.status_code == 409

    other_tenant = create_tenant(database_session, "other-settings-tenant")
    foreign_role = Role(
        tenant_id=other_tenant.id,
        key="custom.foreign",
        name="Foreign Role",
        is_system=False,
    )
    database_session.add(foreign_role)
    database_session.commit()
    create_foreign = client.post(
        "/api/settings/users",
        headers=headers(token),
        json={
            "display_name": "Foreign Attempt",
            "email": "foreign@example.com",
            "login_id": "foreign.user",
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            "role_id": str(foreign_role.id),
        },
    )
    assert create_foreign.status_code == 400
