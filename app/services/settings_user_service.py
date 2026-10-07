import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.permissions import RoleKey
from app.core.security import WorkspaceContext
from app.models.membership import TenantMembership
from app.models.user import User
from app.repositories.settings_repository import (
    PaginatedUsers,
    SettingsUserRecord,
    count_active_memberships_for_user,
    get_role_for_tenant,
    get_settings_user_for_update,
    list_settings_users,
)
from app.repositories.user_repository import get_user_by_email, get_user_by_login_id
from app.repositories.workspace_repository import (
    add_authorization_audit,
    count_active_owners,
    lock_tenant,
    revoke_delegated_permissions,
)
from app.schemas.settings_user import (
    AdminPasswordResetRequest,
    UserSetupCreateRequest,
    UserSetupUpdateRequest,
)
from app.services.menu_permission_service import (
    PermissionEscalationError,
    validate_role_assignment,
)
from app.utils.identifiers import normalize_email, normalize_login_id
from app.utils.password import hash_password


class SettingsUserNotFoundError(Exception):
    pass


class DuplicateSettingsUserError(Exception):
    pass


class InvalidSettingsRoleError(Exception):
    pass


class MultiTenantAccountLimitationError(Exception):
    pass


class LastSettingsOwnerError(Exception):
    pass


class SelfEscalationError(Exception):
    pass


def get_settings_users(
    database_session: Session,
    context: WorkspaceContext,
    page: int,
    size: int,
) -> PaginatedUsers:
    return list_settings_users(
        database_session,
        context.tenant.id,
        offset=(page - 1) * size,
        limit=size,
    )


def create_settings_user(
    database_session: Session,
    context: WorkspaceContext,
    payload: UserSetupCreateRequest,
) -> SettingsUserRecord:
    lock_tenant(database_session, context.tenant.id)
    email = normalize_email(str(payload.email))
    login_id = normalize_login_id(payload.login_id)
    if (
        get_user_by_email(database_session, email) is not None
        or get_user_by_login_id(database_session, login_id) is not None
    ):
        database_session.rollback()
        raise DuplicateSettingsUserError

    role = get_role_for_tenant(database_session, context.tenant.id, payload.role_id)
    if role is None:
        database_session.rollback()
        raise InvalidSettingsRoleError
    try:
        validate_role_assignment(database_session, context, role)
    except PermissionEscalationError:
        database_session.rollback()
        raise

    user = User(
        name=payload.display_name,
        email=email,
        login_id=login_id,
        password_hash=hash_password(payload.password),
    )
    database_session.add(user)
    database_session.flush()
    membership = TenantMembership(
        tenant_id=context.tenant.id,
        user_id=user.id,
        role_id=role.id,
    )
    database_session.add(membership)
    database_session.flush()
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="settings_user.created",
        target_membership_id=membership.id,
        details={"user_id": str(user.id), "role_id": str(role.id)},
    )
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        raise DuplicateSettingsUserError from exception
    database_session.refresh(user)
    database_session.refresh(membership)
    return SettingsUserRecord(user, membership, role)


def update_settings_user(
    database_session: Session,
    context: WorkspaceContext,
    user_id: uuid.UUID,
    payload: UserSetupUpdateRequest,
) -> SettingsUserRecord:
    lock_tenant(database_session, context.tenant.id)
    target = get_settings_user_for_update(
        database_session,
        context.tenant.id,
        user_id,
    )
    if target is None or not target.membership.is_active:
        database_session.rollback()
        raise SettingsUserNotFoundError

    new_role = target.role
    if payload.role_id is not None and payload.role_id != target.role.id:
        if target.user.id == context.user.id:
            database_session.rollback()
            raise SelfEscalationError
        new_role = get_role_for_tenant(
            database_session,
            context.tenant.id,
            payload.role_id,
        )
        if new_role is None:
            database_session.rollback()
            raise InvalidSettingsRoleError
        validate_role_assignment(database_session, context, new_role)
        if (
            target.role.key == RoleKey.OWNER.value
            and new_role.key != RoleKey.OWNER.value
            and count_active_owners(database_session, context.tenant.id) <= 1
        ):
            database_session.rollback()
            raise LastSettingsOwnerError
        target.membership.role_id = new_role.id
        revoke_delegated_permissions(
            database_session,
            target.membership.id,
            datetime.now(timezone.utc),
        )

    if payload.display_name is not None:
        if count_active_memberships_for_user(database_session, target.user.id) > 1:
            database_session.rollback()
            raise MultiTenantAccountLimitationError
        target.user.name = payload.display_name

    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="settings_user.updated",
        target_membership_id=target.membership.id,
        details={
            "user_id": str(target.user.id),
            "role_id": str(new_role.id),
            "display_name_changed": str(payload.display_name is not None),
        },
    )
    database_session.commit()
    database_session.refresh(target.user)
    database_session.refresh(target.membership)
    return SettingsUserRecord(target.user, target.membership, new_role)


def revoke_settings_user(
    database_session: Session,
    context: WorkspaceContext,
    user_id: uuid.UUID,
) -> None:
    lock_tenant(database_session, context.tenant.id)
    target = get_settings_user_for_update(
        database_session,
        context.tenant.id,
        user_id,
    )
    if target is None or not target.membership.is_active:
        database_session.rollback()
        raise SettingsUserNotFoundError
    if (
        target.role.key == RoleKey.OWNER.value
        and count_active_owners(database_session, context.tenant.id) <= 1
    ):
        database_session.rollback()
        raise LastSettingsOwnerError

    target.membership.is_active = False
    target.membership.revoked_at = datetime.now(timezone.utc)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="settings_user.revoked",
        target_membership_id=target.membership.id,
        details={"user_id": str(target.user.id), "role_id": str(target.role.id)},
    )
    database_session.commit()


def reset_settings_user_password(
    database_session: Session,
    context: WorkspaceContext,
    user_id: uuid.UUID,
    payload: AdminPasswordResetRequest,
) -> None:
    target = get_settings_user_for_update(
        database_session,
        context.tenant.id,
        user_id,
    )
    if target is None or not target.membership.is_active:
        database_session.rollback()
        raise SettingsUserNotFoundError
    if count_active_memberships_for_user(database_session, target.user.id) > 1:
        database_session.rollback()
        raise MultiTenantAccountLimitationError

    target.user.password_hash = hash_password(payload.password)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="settings_user.password_reset",
        target_membership_id=target.membership.id,
        details={"user_id": str(target.user.id)},
    )
    database_session.commit()
