import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.permissions import PermissionKey, RoleKey
from app.models.authorization_audit import AuthorizationAudit
from app.models.membership import MembershipPermission, TenantMembership
from app.models.role import Permission, Role, RolePermission
from app.models.tenant import Tenant
from app.models.user import User


@dataclass(frozen=True)
class MembershipRecord:
    membership: TenantMembership
    tenant: Tenant
    role: Role


@dataclass(frozen=True)
class MemberRecord:
    membership: TenantMembership
    user: User
    role: Role


def _membership_record(statement: object, database_session: Session) -> MembershipRecord | None:
    row = database_session.execute(statement).one_or_none()
    if row is None:
        return None
    return MembershipRecord(row[0], row[1], row[2])


def list_active_memberships_for_user(
    database_session: Session,
    user_id: uuid.UUID,
) -> list[MembershipRecord]:
    rows = database_session.execute(
        select(TenantMembership, Tenant, Role)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .join(Role, Role.id == TenantMembership.role_id)
        .where(
            TenantMembership.user_id == user_id,
            TenantMembership.is_active.is_(True),
            TenantMembership.revoked_at.is_(None),
            Tenant.is_active.is_(True),
        )
        .order_by(Tenant.name, Tenant.id)
    ).all()
    return [MembershipRecord(row[0], row[1], row[2]) for row in rows]


def get_active_membership(
    database_session: Session,
    *,
    membership_id: uuid.UUID,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> MembershipRecord | None:
    statement = (
        select(TenantMembership, Tenant, Role)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .join(Role, Role.id == TenantMembership.role_id)
        .where(
            TenantMembership.id == membership_id,
            TenantMembership.user_id == user_id,
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.is_active.is_(True),
            TenantMembership.revoked_at.is_(None),
            Tenant.is_active.is_(True),
        )
    )
    return _membership_record(statement, database_session)


def get_active_membership_for_workspace(
    database_session: Session,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> MembershipRecord | None:
    statement = (
        select(TenantMembership, Tenant, Role)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .join(Role, Role.id == TenantMembership.role_id)
        .where(
            TenantMembership.user_id == user_id,
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.is_active.is_(True),
            TenantMembership.revoked_at.is_(None),
            Tenant.is_active.is_(True),
        )
    )
    return _membership_record(statement, database_session)


def get_permission_keys(
    database_session: Session,
    membership: TenantMembership,
) -> frozenset[str]:
    role_permissions = database_session.scalars(
        select(Permission.key)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == membership.role_id)
    ).all()
    delegated_permissions = database_session.scalars(
        select(Permission.key)
        .join(
            MembershipPermission,
            MembershipPermission.permission_id == Permission.id,
        )
        .where(
            MembershipPermission.membership_id == membership.id,
            MembershipPermission.revoked_at.is_(None),
        )
    ).all()
    return frozenset(role_permissions) | frozenset(delegated_permissions)


def get_role_by_key(database_session: Session, role_key: RoleKey | str) -> Role | None:
    return database_session.scalar(select(Role).where(Role.key == str(role_key)))


def get_permission_by_key(
    database_session: Session,
    permission_key: PermissionKey | str,
) -> Permission | None:
    return database_session.scalar(
        select(Permission).where(Permission.key == str(permission_key))
    )


def get_tenant_by_slug(database_session: Session, slug: str) -> Tenant | None:
    return database_session.scalar(select(Tenant).where(Tenant.slug == slug))


def lock_tenant(database_session: Session, tenant_id: uuid.UUID) -> Tenant | None:
    return database_session.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )


def create_tenant(database_session: Session, name: str, slug: str) -> Tenant:
    tenant = Tenant(name=name, slug=slug)
    database_session.add(tenant)
    database_session.flush()
    return tenant


def create_membership(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
) -> TenantMembership:
    membership = TenantMembership(
        tenant_id=tenant_id,
        user_id=user_id,
        role_id=role_id,
    )
    database_session.add(membership)
    database_session.flush()
    return membership


def list_tenant_members(
    database_session: Session,
    tenant_id: uuid.UUID,
) -> list[MemberRecord]:
    rows = database_session.execute(
        select(TenantMembership, User, Role)
        .join(User, User.id == TenantMembership.user_id)
        .join(Role, Role.id == TenantMembership.role_id)
        .where(TenantMembership.tenant_id == tenant_id)
        .order_by(User.name, User.id)
    ).all()
    return [MemberRecord(row[0], row[1], row[2]) for row in rows]


def get_tenant_member_for_update(
    database_session: Session,
    tenant_id: uuid.UUID,
    membership_id: uuid.UUID,
) -> MemberRecord | None:
    row = database_session.execute(
        select(TenantMembership, User, Role)
        .join(User, User.id == TenantMembership.user_id)
        .join(Role, Role.id == TenantMembership.role_id)
        .where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.id == membership_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        return None
    return MemberRecord(row[0], row[1], row[2])


def count_active_owners(database_session: Session, tenant_id: uuid.UUID) -> int:
    return database_session.scalar(
        select(func.count(TenantMembership.id))
        .join(Role, Role.id == TenantMembership.role_id)
        .where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.is_active.is_(True),
            TenantMembership.revoked_at.is_(None),
            Role.key == RoleKey.OWNER.value,
        )
    ) or 0


def add_authorization_audit(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    action: str,
    target_membership_id: uuid.UUID | None,
    details: dict[str, Any],
) -> None:
    database_session.add(
        AuthorizationAudit(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            target_membership_id=target_membership_id,
            details=details,
        )
    )


def delegate_permission(
    database_session: Session,
    *,
    membership_id: uuid.UUID,
    permission_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    grant = database_session.get(
        MembershipPermission,
        (membership_id, permission_id),
    )
    if grant is None:
        grant = MembershipPermission(
            membership_id=membership_id,
            permission_id=permission_id,
            granted_by_user_id=actor_user_id,
        )
        database_session.add(grant)
    else:
        grant.granted_by_user_id = actor_user_id
        grant.granted_at = datetime.now(timezone.utc)
        grant.revoked_at = None


def revoke_delegated_permissions(
    database_session: Session,
    membership_id: uuid.UUID,
    revoked_at: datetime,
) -> None:
    database_session.execute(
        update(MembershipPermission)
        .where(
            MembershipPermission.membership_id == membership_id,
            MembershipPermission.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )
