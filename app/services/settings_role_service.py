import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import WorkspaceContext
from app.models.role import Role
from app.repositories.settings_repository import (
    PaginatedRoles,
    count_members_for_role,
    get_role_for_tenant,
    list_roles,
    role_name_exists,
)
from app.repositories.workspace_repository import add_authorization_audit, lock_tenant


class SettingsRoleNotFoundError(Exception):
    pass


class DuplicateRoleNameError(Exception):
    pass


class SystemRoleImmutableError(Exception):
    pass


class AssignedRoleConflictError(Exception):
    pass


def normalize_role_name(name: str) -> str:
    return " ".join(name.strip().split()).casefold()


def get_roles(
    database_session: Session,
    context: WorkspaceContext,
    page: int,
    size: int,
) -> PaginatedRoles:
    return list_roles(
        database_session,
        context.tenant.id,
        offset=(page - 1) * size,
        limit=size,
    )


def create_role(
    database_session: Session,
    context: WorkspaceContext,
    name: str,
) -> Role:
    lock_tenant(database_session, context.tenant.id)
    normalized_name = normalize_role_name(name)
    if role_name_exists(database_session, context.tenant.id, normalized_name):
        database_session.rollback()
        raise DuplicateRoleNameError

    role = Role(
        tenant_id=context.tenant.id,
        key=f"custom.{uuid.uuid4().hex}",
        name=name,
        normalized_name=normalized_name,
        is_system=False,
    )
    database_session.add(role)
    database_session.flush()
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="role.created",
        target_membership_id=None,
        details={"role_id": str(role.id), "name": role.name},
    )
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        raise DuplicateRoleNameError from exception
    database_session.refresh(role)
    return role


def update_role(
    database_session: Session,
    context: WorkspaceContext,
    role_id: uuid.UUID,
    name: str,
) -> Role:
    lock_tenant(database_session, context.tenant.id)
    role = get_role_for_tenant(
        database_session,
        context.tenant.id,
        role_id,
        for_update=True,
    )
    if role is None:
        database_session.rollback()
        raise SettingsRoleNotFoundError
    if role.is_system:
        database_session.rollback()
        raise SystemRoleImmutableError

    normalized_name = normalize_role_name(name)
    if role_name_exists(
        database_session,
        context.tenant.id,
        normalized_name,
        exclude_role_id=role.id,
    ):
        database_session.rollback()
        raise DuplicateRoleNameError
    previous_name = role.name
    role.name = name
    role.normalized_name = normalized_name
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="role.updated",
        target_membership_id=None,
        details={
            "role_id": str(role.id),
            "previous_name": previous_name,
            "new_name": role.name,
        },
    )
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        raise DuplicateRoleNameError from exception
    database_session.refresh(role)
    return role


def delete_role(
    database_session: Session,
    context: WorkspaceContext,
    role_id: uuid.UUID,
) -> None:
    lock_tenant(database_session, context.tenant.id)
    role = get_role_for_tenant(
        database_session,
        context.tenant.id,
        role_id,
        for_update=True,
    )
    if role is None:
        database_session.rollback()
        raise SettingsRoleNotFoundError
    if role.is_system:
        database_session.rollback()
        raise SystemRoleImmutableError
    if count_members_for_role(database_session, context.tenant.id, role.id):
        database_session.rollback()
        raise AssignedRoleConflictError

    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="role.deleted",
        target_membership_id=None,
        details={"role_id": str(role.id), "name": role.name},
    )
    database_session.delete(role)
    database_session.commit()
