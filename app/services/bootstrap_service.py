import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import PermissionKey, RoleKey
from app.models.membership import TenantMembership
from app.models.role import Permission, RolePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.user_repository import get_user_by_identifier
from app.repositories.workspace_repository import (
    add_authorization_audit,
    create_membership,
    create_tenant,
    get_role_by_key,
    get_tenant_by_slug,
)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIRED_OWNER_SETTINGS_PERMISSIONS = frozenset(
    {
        PermissionKey.SETTINGS_ROLES_VIEW.value,
        PermissionKey.SETTINGS_ROLES_CREATE.value,
        PermissionKey.SETTINGS_ROLES_UPDATE.value,
        PermissionKey.SETTINGS_ROLES_DELETE.value,
        PermissionKey.SETTINGS_USERS_VIEW.value,
        PermissionKey.SETTINGS_USERS_CREATE.value,
        PermissionKey.SETTINGS_USERS_UPDATE.value,
        PermissionKey.SETTINGS_USERS_DELETE.value,
        PermissionKey.SETTINGS_USERS_RESET_PASSWORD.value,
        PermissionKey.SETTINGS_MENU_PERMISSIONS_VIEW.value,
        PermissionKey.SETTINGS_MENU_PERMISSIONS_UPDATE.value,
    }
)


class BootstrapUserNotFoundError(Exception):
    pass


class BootstrapPrerequisiteError(Exception):
    pass


class InvalidWorkspaceNameError(Exception):
    pass


class InvalidWorkspaceSlugError(Exception):
    pass


@dataclass(frozen=True)
class BootstrapResult:
    user: User
    tenant: Tenant
    membership: TenantMembership
    created_tenant: bool
    created_membership: bool
    repaired_permission_keys: tuple[str, ...]


def workspace_slug_from_name(workspace_name: str) -> str:
    normalized = unicodedata.normalize("NFKD", workspace_name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")
    slug = slug[:80].rstrip("-")
    if not slug:
        raise InvalidWorkspaceSlugError(
            "Workspace name cannot produce a slug; supply --workspace-slug"
        )
    return slug


def normalize_workspace_slug(workspace_slug: str) -> str:
    slug = workspace_slug.strip().casefold()
    if len(slug) > 80 or not SLUG_PATTERN.fullmatch(slug):
        raise InvalidWorkspaceSlugError(
            "Workspace slug must be at most 80 lowercase letters, numbers, or hyphens"
        )
    return slug


def _normalize_workspace_name(workspace_name: str) -> str:
    name = " ".join(workspace_name.strip().split())
    if not name or len(name) > 120:
        raise InvalidWorkspaceNameError(
            "Workspace name must contain between 1 and 120 characters"
        )
    return name


def _lock_user(database_session: Session, identifier: str) -> User:
    user = get_user_by_identifier(database_session, identifier.strip())
    if user is None:
        raise BootstrapUserNotFoundError
    locked_user = database_session.scalar(
        select(User).where(User.id == user.id).with_for_update()
    )
    if locked_user is None:
        raise BootstrapUserNotFoundError
    return locked_user


def _ensure_owner_permissions(database_session: Session, owner_role_id: uuid.UUID) -> tuple[str, ...]:
    permissions = database_session.scalars(
        select(Permission).where(
            Permission.key.in_(REQUIRED_OWNER_SETTINGS_PERMISSIONS)
        )
    ).all()
    permissions_by_key = {permission.key: permission for permission in permissions}
    missing_definitions = REQUIRED_OWNER_SETTINGS_PERMISSIONS - permissions_by_key.keys()
    if missing_definitions:
        raise BootstrapPrerequisiteError(
            "Required settings permissions are missing; run 'alembic upgrade head' first"
        )

    existing_ids = set(
        database_session.scalars(
            select(RolePermission.permission_id).where(
                RolePermission.role_id == owner_role_id,
                RolePermission.permission_id.in_(
                    permission.id for permission in permissions
                ),
            )
        ).all()
    )
    repaired_keys: list[str] = []
    for permission in permissions:
        if permission.id in existing_ids:
            continue
        database_session.add(
            RolePermission(
                role_id=owner_role_id,
                permission_id=permission.id,
            )
        )
        repaired_keys.append(permission.key)
    return tuple(sorted(repaired_keys))


def bootstrap_owner_workspace(
    database_session: Session,
    *,
    identifier: str,
    workspace_name: str,
    workspace_slug: str | None = None,
) -> BootstrapResult:
    """Initialize local tenant ownership inside the caller's transaction."""

    name = _normalize_workspace_name(workspace_name)
    slug = normalize_workspace_slug(workspace_slug or workspace_slug_from_name(name))
    user = _lock_user(database_session, identifier)
    if not user.is_active:
        raise BootstrapPrerequisiteError("The supplied user account is inactive")

    owner_role = get_role_by_key(database_session, RoleKey.OWNER)
    if owner_role is None or not owner_role.is_system:
        raise BootstrapPrerequisiteError(
            "Owner system role is missing; run 'alembic upgrade head' first"
        )
    repaired_permission_keys = _ensure_owner_permissions(
        database_session,
        owner_role.id,
    )

    tenant = get_tenant_by_slug(database_session, slug)
    created_tenant = tenant is None
    tenant_changed = False
    if tenant is None:
        tenant = create_tenant(database_session, name, slug)
    else:
        tenant = database_session.scalar(
            select(Tenant).where(Tenant.id == tenant.id).with_for_update()
        )
        if tenant is None:
            raise BootstrapPrerequisiteError("Workspace disappeared during bootstrap")
        if tenant.name != name:
            tenant.name = name
            tenant_changed = True
        if not tenant.is_active:
            tenant.is_active = True
            tenant_changed = True

    membership = database_session.scalar(
        select(TenantMembership)
        .where(
            TenantMembership.tenant_id == tenant.id,
            TenantMembership.user_id == user.id,
        )
        .with_for_update()
    )
    created_membership = membership is None
    membership_changed = False
    previous_role_id: str | None = None
    if membership is None:
        membership = create_membership(
            database_session,
            tenant_id=tenant.id,
            user_id=user.id,
            role_id=owner_role.id,
        )
    else:
        if membership.role_id != owner_role.id:
            previous_role_id = str(membership.role_id)
            membership.role_id = owner_role.id
            membership_changed = True
        if not membership.is_active or membership.revoked_at is not None:
            membership.is_active = True
            membership.revoked_at = None
            membership_changed = True

    changed = (
        created_tenant
        or tenant_changed
        or created_membership
        or membership_changed
        or bool(repaired_permission_keys)
    )
    if changed:
        add_authorization_audit(
            database_session,
            tenant_id=tenant.id,
            actor_user_id=user.id,
            action="workspace.bootstrap_owner",
            target_membership_id=membership.id,
            details={
                "created_tenant": created_tenant,
                "created_membership": created_membership,
                "previous_role_id": previous_role_id,
                "repaired_permission_keys": list(repaired_permission_keys),
                "performed_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    database_session.flush()
    return BootstrapResult(
        user=user,
        tenant=tenant,
        membership=membership,
        created_tenant=created_tenant,
        created_membership=created_membership,
        repaired_permission_keys=repaired_permission_keys,
    )
