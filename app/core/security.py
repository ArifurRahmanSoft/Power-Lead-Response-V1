import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.jwt import AccessTokenError, decode_access_token
from app.core.permissions import PermissionKey, RoleKey
from app.models.membership import TenantMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.user_repository import get_user_by_id
from app.repositories.workspace_repository import (
    get_active_membership,
    get_permission_keys,
)

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedIdentity:
    user: User
    scope: str
    tenant_id: uuid.UUID | None
    membership_id: uuid.UUID | None


@dataclass(frozen=True)
class WorkspaceContext:
    user: User
    tenant: Tenant
    membership: TenantMembership
    role: str
    permissions: frozenset[str]


def _authentication_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_token_payload(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _authentication_error()

    try:
        return decode_access_token(credentials.credentials)
    except AccessTokenError as exception:
        raise _authentication_error() from exception


def get_authenticated_identity(
    token_payload: Annotated[dict[str, Any], Depends(get_token_payload)],
    database_session: Annotated[Session, Depends(get_db)],
) -> AuthenticatedIdentity:
    try:
        user_id = uuid.UUID(str(token_payload["sub"]))
        tenant_id = (
            uuid.UUID(str(token_payload["tenant_id"]))
            if token_payload.get("tenant_id")
            else None
        )
        membership_id = (
            uuid.UUID(str(token_payload["membership_id"]))
            if token_payload.get("membership_id")
            else None
        )
    except (KeyError, TypeError, ValueError) as exception:
        raise _authentication_error() from exception

    user = get_user_by_id(database_session, user_id)
    if user is None or not user.is_active:
        raise _authentication_error()

    return AuthenticatedIdentity(
        user=user,
        scope=str(token_payload.get("scope", "onboarding")),
        tenant_id=tenant_id,
        membership_id=membership_id,
    )


def get_workspace_context(
    identity: Annotated[AuthenticatedIdentity, Depends(get_authenticated_identity)],
    database_session: Annotated[Session, Depends(get_db)],
) -> WorkspaceContext:
    if (
        identity.scope != "workspace"
        or identity.tenant_id is None
        or identity.membership_id is None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An active workspace must be selected",
        )

    record = get_active_membership(
        database_session,
        membership_id=identity.membership_id,
        user_id=identity.user.id,
        tenant_id=identity.tenant_id,
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace access is no longer active",
        )

    return WorkspaceContext(
        user=identity.user,
        tenant=record.tenant,
        membership=record.membership,
        role=record.role.key,
        permissions=get_permission_keys(database_session, record.membership),
    )


def require_permission(
    permission: PermissionKey,
) -> Callable[..., WorkspaceContext]:
    def dependency(
        context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
    ) -> WorkspaceContext:
        if permission.value not in context.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient workspace permission",
            )
        return context

    return dependency


def require_workspace_permission(
    permission: PermissionKey,
) -> Callable[..., WorkspaceContext]:
    permission_dependency = require_permission(permission)

    def dependency(
        workspace_id: uuid.UUID,
        context: Annotated[WorkspaceContext, Depends(permission_dependency)],
    ) -> WorkspaceContext:
        if workspace_id != context.tenant.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-workspace access is forbidden",
            )
        return context

    return dependency


def enforce_lead_scope(
    context: WorkspaceContext,
    *,
    lead_tenant_id: uuid.UUID,
    assigned_user_id: uuid.UUID | None,
    write: bool,
) -> None:
    if lead_tenant_id != context.tenant.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-workspace access is forbidden",
        )
    if PermissionKey.LEADS_READ.value not in context.permissions:
        raise HTTPException(status_code=403, detail="Lead access is forbidden")
    if not write:
        return
    if context.role in {RoleKey.OWNER.value, RoleKey.OPERATOR.value}:
        if PermissionKey.LEADS_MANAGE.value in context.permissions:
            return
    elif context.role == RoleKey.STAFF.value:
        if (
            assigned_user_id == context.user.id
            and PermissionKey.LEADS_ASSIGNED_UPDATE.value in context.permissions
        ):
            return
    raise HTTPException(status_code=403, detail="Lead update is forbidden")
