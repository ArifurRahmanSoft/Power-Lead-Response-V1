import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.jwt import create_access_token
from app.core.security import AuthenticatedIdentity
from app.models.user import User
from app.repositories.user_repository import get_user_by_identifier
from app.repositories.workspace_repository import (
    MembershipRecord,
    get_active_membership,
    get_active_membership_for_workspace,
    get_permission_keys,
    list_active_memberships_for_user,
)
from app.schemas.auth import AuthStatus, LoginRequest
from app.utils.password import hash_password, verify_password


class InvalidCredentialsError(Exception):
    pass


class InvalidWorkspaceSelectionError(Exception):
    pass


class InactiveWorkspaceAccessError(Exception):
    pass


@dataclass(frozen=True)
class AuthenticationResult:
    user: User
    auth_status: AuthStatus
    memberships: tuple[MembershipRecord, ...]
    selected_membership: MembershipRecord | None
    permissions: tuple[str, ...]
    access_token: str | None


# This non-secret hash keeps unknown-user and wrong-password paths comparable.
_DUMMY_PASSWORD_HASH = hash_password("login-timing-placeholder")


def _build_result(
    database_session: Session,
    user: User,
    *,
    selected_membership: MembershipRecord | None = None,
    issue_token: bool,
) -> AuthenticationResult:
    memberships = tuple(list_active_memberships_for_user(database_session, user.id))

    if selected_membership is not None:
        auth_status = AuthStatus.WORKSPACE_SELECTED
        permissions = tuple(
            sorted(
                get_permission_keys(
                    database_session,
                    selected_membership.membership,
                )
            )
        )
        scope = "workspace"
    elif memberships:
        auth_status = AuthStatus.WORKSPACE_SELECTION_REQUIRED
        permissions = ()
        scope = "workspace_selection"
    else:
        auth_status = AuthStatus.ONBOARDING_PENDING
        permissions = ()
        scope = "onboarding"

    access_token = None
    if issue_token:
        access_token = create_access_token(
            user.id,
            user.email,
            scope=scope,
            tenant_id=(
                selected_membership.tenant.id
                if selected_membership is not None
                else None
            ),
            membership_id=(
                selected_membership.membership.id
                if selected_membership is not None
                else None
            ),
        )

    return AuthenticationResult(
        user=user,
        auth_status=auth_status,
        memberships=memberships,
        selected_membership=selected_membership,
        permissions=permissions,
        access_token=access_token,
    )


def authenticate_user(
    database_session: Session,
    payload: LoginRequest,
) -> AuthenticationResult:
    identifier = payload.identifier or str(payload.email)
    user = get_user_by_identifier(database_session, identifier)

    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_is_valid = verify_password(payload.password, password_hash)

    if user is None or not user.is_active or not password_is_valid:
        raise InvalidCredentialsError

    memberships = list_active_memberships_for_user(database_session, user.id)
    selected_membership = memberships[0] if len(memberships) == 1 else None
    return _build_result(
        database_session,
        user,
        selected_membership=selected_membership,
        issue_token=True,
    )


def select_workspace(
    database_session: Session,
    user: User,
    workspace_id: uuid.UUID,
) -> AuthenticationResult:
    membership = get_active_membership_for_workspace(
        database_session,
        user_id=user.id,
        tenant_id=workspace_id,
    )
    if membership is None:
        raise InvalidWorkspaceSelectionError
    return _build_result(
        database_session,
        user,
        selected_membership=membership,
        issue_token=True,
    )


def get_current_session(
    database_session: Session,
    identity: AuthenticatedIdentity,
) -> AuthenticationResult:
    selected_membership = None
    if identity.scope == "workspace":
        if identity.tenant_id is None or identity.membership_id is None:
            raise InactiveWorkspaceAccessError
        selected_membership = get_active_membership(
            database_session,
            membership_id=identity.membership_id,
            user_id=identity.user.id,
            tenant_id=identity.tenant_id,
        )
        if selected_membership is None:
            raise InactiveWorkspaceAccessError

    return _build_result(
        database_session,
        identity.user,
        selected_membership=selected_membership,
        issue_token=False,
    )
