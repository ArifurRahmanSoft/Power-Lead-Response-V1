import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models.membership import TenantMembership
from app.models.role import Permission, Role, RolePermission
from app.models.user import User


@dataclass(frozen=True)
class PaginatedRoles:
    items: list[Role]
    total: int


@dataclass(frozen=True)
class SettingsUserRecord:
    user: User
    membership: TenantMembership
    role: Role


@dataclass(frozen=True)
class PaginatedUsers:
    items: list[SettingsUserRecord]
    total: int


def _allowed_role_clause(tenant_id: uuid.UUID) -> object:
    return or_(Role.is_system.is_(True), Role.tenant_id == tenant_id)


def list_roles(
    database_session: Session,
    tenant_id: uuid.UUID,
    *,
    offset: int,
    limit: int,
) -> PaginatedRoles:
    condition = _allowed_role_clause(tenant_id)
    total = database_session.scalar(select(func.count(Role.id)).where(condition)) or 0
    items = list(
        database_session.scalars(
            select(Role)
            .where(condition)
            .order_by(Role.is_system.desc(), Role.name, Role.id)
            .offset(offset)
            .limit(limit)
        ).all()
    )
    return PaginatedRoles(items=items, total=total)


def get_role_for_tenant(
    database_session: Session,
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Role | None:
    statement = select(Role).where(
        Role.id == role_id,
        _allowed_role_clause(tenant_id),
    )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def role_name_exists(
    database_session: Session,
    tenant_id: uuid.UUID,
    normalized_name: str,
    *,
    exclude_role_id: uuid.UUID | None = None,
) -> bool:
    statement = select(Role.id).where(
        Role.normalized_name == normalized_name,
        _allowed_role_clause(tenant_id),
    )
    if exclude_role_id is not None:
        statement = statement.where(Role.id != exclude_role_id)
    return database_session.scalar(statement) is not None


def count_active_members_for_role(
    database_session: Session,
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
) -> int:
    return database_session.scalar(
        select(func.count(TenantMembership.id)).where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.role_id == role_id,
            TenantMembership.is_active.is_(True),
            TenantMembership.revoked_at.is_(None),
        )
    ) or 0


def count_members_for_role(
    database_session: Session,
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
) -> int:
    return database_session.scalar(
        select(func.count(TenantMembership.id)).where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.role_id == role_id,
        )
    ) or 0


def get_role_permission_keys(
    database_session: Session,
    role_id: uuid.UUID,
) -> frozenset[str]:
    return frozenset(
        database_session.scalars(
            select(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
        ).all()
    )


def replace_role_permissions(
    database_session: Session,
    role_id: uuid.UUID,
    permission_keys: set[str],
    managed_permission_keys: frozenset[str],
) -> None:
    permissions = list(
        database_session.scalars(
            select(Permission).where(Permission.key.in_(permission_keys))
        ).all()
    )
    managed_permission_ids = select(Permission.id).where(
        Permission.key.in_(managed_permission_keys)
    )
    database_session.execute(
        delete(RolePermission).where(
            RolePermission.role_id == role_id,
            RolePermission.permission_id.in_(managed_permission_ids),
        )
    )
    database_session.add_all(
        RolePermission(role_id=role_id, permission_id=permission.id)
        for permission in permissions
    )


def list_settings_users(
    database_session: Session,
    tenant_id: uuid.UUID,
    *,
    offset: int,
    limit: int,
) -> PaginatedUsers:
    condition = TenantMembership.tenant_id == tenant_id
    total = database_session.scalar(
        select(func.count(TenantMembership.id)).where(condition)
    ) or 0
    rows = database_session.execute(
        select(User, TenantMembership, Role)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(Role, Role.id == TenantMembership.role_id)
        .where(condition)
        .order_by(User.name, User.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return PaginatedUsers(
        items=[SettingsUserRecord(row[0], row[1], row[2]) for row in rows],
        total=total,
    )


def get_settings_user_for_update(
    database_session: Session,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> SettingsUserRecord | None:
    row = database_session.execute(
        select(User, TenantMembership, Role)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(Role, Role.id == TenantMembership.role_id)
        .where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.user_id == user_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None:
        return None
    return SettingsUserRecord(row[0], row[1], row[2])


def count_active_memberships_for_user(
    database_session: Session,
    user_id: uuid.UUID,
) -> int:
    return database_session.scalar(
        select(func.count(TenantMembership.id)).where(
            TenantMembership.user_id == user_id,
            TenantMembership.is_active.is_(True),
            TenantMembership.revoked_at.is_(None),
        )
    ) or 0
