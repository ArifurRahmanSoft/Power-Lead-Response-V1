import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.permissions import PermissionKey, RoleKey
from app.core.security import WorkspaceContext
from app.repositories.workspace_repository import (
    MemberRecord,
    add_authorization_audit,
    count_active_owners,
    delegate_permission,
    get_permission_by_key,
    get_role_by_key,
    get_tenant_member_for_update,
    list_tenant_members,
    lock_tenant,
    revoke_delegated_permissions,
)


class WorkspaceMemberNotFoundError(Exception):
    pass


class LastActiveOwnerError(Exception):
    pass


class InvalidPermissionDelegationError(Exception):
    pass


def get_workspace_members(
    database_session: Session,
    context: WorkspaceContext,
) -> list[MemberRecord]:
    return list_tenant_members(database_session, context.tenant.id)


def assign_member_role(
    database_session: Session,
    context: WorkspaceContext,
    membership_id: uuid.UUID,
    role_key: RoleKey,
) -> MemberRecord:
    lock_tenant(database_session, context.tenant.id)
    target = get_tenant_member_for_update(
        database_session,
        context.tenant.id,
        membership_id,
    )
    role = get_role_by_key(database_session, role_key)
    if target is None or role is None or not target.membership.is_active:
        database_session.rollback()
        raise WorkspaceMemberNotFoundError

    if (
        target.role.key == RoleKey.OWNER.value
        and role_key != RoleKey.OWNER
        and count_active_owners(database_session, context.tenant.id) <= 1
    ):
        database_session.rollback()
        raise LastActiveOwnerError

    previous_role = target.role.key
    target.membership.role_id = role.id
    revoke_delegated_permissions(
        database_session,
        target.membership.id,
        datetime.now(timezone.utc),
    )
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="membership.role_changed",
        target_membership_id=target.membership.id,
        details={"previous_role": previous_role, "new_role": role.key},
    )
    database_session.commit()
    database_session.refresh(target.membership)
    return MemberRecord(target.membership, target.user, role)


def revoke_member(
    database_session: Session,
    context: WorkspaceContext,
    membership_id: uuid.UUID,
) -> None:
    lock_tenant(database_session, context.tenant.id)
    target = get_tenant_member_for_update(
        database_session,
        context.tenant.id,
        membership_id,
    )
    if target is None or not target.membership.is_active:
        database_session.rollback()
        raise WorkspaceMemberNotFoundError
    if (
        target.role.key == RoleKey.OWNER.value
        and count_active_owners(database_session, context.tenant.id) <= 1
    ):
        database_session.rollback()
        raise LastActiveOwnerError

    target.membership.is_active = False
    target.membership.revoked_at = datetime.now(timezone.utc)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="membership.revoked",
        target_membership_id=target.membership.id,
        details={"previous_role": target.role.key},
    )
    database_session.commit()


def delegate_operator_integration_access(
    database_session: Session,
    context: WorkspaceContext,
    membership_id: uuid.UUID,
) -> None:
    target = get_tenant_member_for_update(
        database_session,
        context.tenant.id,
        membership_id,
    )
    permission = get_permission_by_key(
        database_session,
        PermissionKey.INTEGRATIONS_USE,
    )
    if (
        target is None
        or permission is None
        or not target.membership.is_active
        or target.membership.revoked_at is not None
        or target.role.key != RoleKey.OPERATOR.value
    ):
        database_session.rollback()
        raise InvalidPermissionDelegationError

    delegate_permission(
        database_session,
        membership_id=target.membership.id,
        permission_id=permission.id,
        actor_user_id=context.user.id,
    )
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="membership.permission_delegated",
        target_membership_id=target.membership.id,
        details={"permission": PermissionKey.INTEGRATIONS_USE.value},
    )
    database_session.commit()
