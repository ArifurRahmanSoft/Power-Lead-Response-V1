import uuid

from sqlalchemy.orm import Session

from app.core.menu_catalog import (
    MENU_CATALOG,
    MENU_PERMISSION_KEYS,
    OWNER_REQUIRED_MANAGEMENT_KEYS,
    PRIVILEGED_MANAGEMENT_KEYS,
)
from app.core.permissions import RoleKey
from app.core.security import WorkspaceContext
from app.models.role import Role
from app.repositories.settings_repository import (
    get_role_for_tenant,
    get_role_permission_keys,
    replace_role_permissions,
)
from app.repositories.workspace_repository import add_authorization_audit, lock_tenant


class MenuRoleNotFoundError(Exception):
    pass


class InvalidPermissionSetError(Exception):
    pass


class PermissionEscalationError(Exception):
    pass


def get_menu_catalog() -> tuple[object, ...]:
    return MENU_CATALOG


def get_menu_permissions_for_role(
    database_session: Session,
    context: WorkspaceContext,
    role_id: uuid.UUID,
) -> tuple[Role, list[str]]:
    role = get_role_for_tenant(database_session, context.tenant.id, role_id)
    if role is None:
        raise MenuRoleNotFoundError
    keys = sorted(
        get_role_permission_keys(database_session, role.id) & MENU_PERMISSION_KEYS
    )
    return role, keys


def _validate_permission_combinations(permission_keys: set[str]) -> None:
    if not permission_keys <= MENU_PERMISSION_KEYS:
        raise InvalidPermissionSetError
    for key in permission_keys:
        menu_key, action = key.rsplit(".", 1)
        if action != "view" and f"{menu_key}.view" not in permission_keys:
            raise InvalidPermissionSetError


def validate_role_assignment(
    database_session: Session,
    context: WorkspaceContext,
    role: Role,
) -> None:
    role_permissions = get_role_permission_keys(database_session, role.id)
    actor_is_owner = context.role == RoleKey.OWNER.value
    if role.key == RoleKey.OWNER.value and not actor_is_owner:
        raise PermissionEscalationError
    if not actor_is_owner:
        if role_permissions & PRIVILEGED_MANAGEMENT_KEYS:
            raise PermissionEscalationError
        if not role_permissions <= context.permissions:
            raise PermissionEscalationError


def update_menu_permissions_for_role(
    database_session: Session,
    context: WorkspaceContext,
    role_id: uuid.UUID,
    permission_keys: list[str],
) -> tuple[Role, list[str]]:
    requested = set(permission_keys)
    _validate_permission_combinations(requested)
    lock_tenant(database_session, context.tenant.id)
    role = get_role_for_tenant(
        database_session,
        context.tenant.id,
        role_id,
        for_update=True,
    )
    if role is None:
        database_session.rollback()
        raise MenuRoleNotFoundError

    actor_is_owner = context.role == RoleKey.OWNER.value
    current = get_role_permission_keys(database_session, role.id) & MENU_PERMISSION_KEYS
    if not actor_is_owner:
        if role.is_system or role.id == context.membership.role_id:
            database_session.rollback()
            raise PermissionEscalationError
        if requested & PRIVILEGED_MANAGEMENT_KEYS:
            database_session.rollback()
            raise PermissionEscalationError
        if not requested <= context.permissions:
            database_session.rollback()
            raise PermissionEscalationError
    if role.key == RoleKey.OWNER.value and not OWNER_REQUIRED_MANAGEMENT_KEYS <= requested:
        database_session.rollback()
        raise InvalidPermissionSetError

    replace_role_permissions(
        database_session,
        role.id,
        requested,
        MENU_PERMISSION_KEYS,
    )
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="role.permissions_updated",
        target_membership_id=None,
        details={
            "role_id": str(role.id),
            "added": sorted(requested - current),
            "removed": sorted(current - requested),
        },
    )
    database_session.commit()
    return role, sorted(requested)
