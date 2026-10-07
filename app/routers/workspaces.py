import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_workspace_permission
from app.repositories.workspace_repository import MemberRecord
from app.schemas.workspace import (
    MembershipActionResponse,
    PermissionDelegationResponse,
    RoleAssignmentRequest,
    RoleAssignmentResponse,
    WorkspaceMember,
    WorkspaceMemberListResponse,
)
from app.services.workspace_service import (
    InvalidPermissionDelegationError,
    LastActiveOwnerError,
    WorkspaceMemberNotFoundError,
    assign_member_role,
    delegate_operator_integration_access,
    get_workspace_members,
    revoke_member,
)

router = APIRouter()
owner_workspace_access = require_workspace_permission(
    PermissionKey.TENANT_USERS_MANAGE
)


def _member_response(record: MemberRecord) -> WorkspaceMember:
    return WorkspaceMember(
        membership_id=record.membership.id,
        user_id=record.user.id,
        login_id=record.user.login_id,
        name=record.user.name,
        email=record.user.email,
        role=record.role.key,
        is_active=(
            record.membership.is_active
            and record.membership.revoked_at is None
        ),
    )


@router.get(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberListResponse,
)
def list_members(
    context: Annotated[WorkspaceContext, Depends(owner_workspace_access)],
    database_session: Annotated[Session, Depends(get_db)],
) -> WorkspaceMemberListResponse:
    return WorkspaceMemberListResponse(
        success=True,
        data=[
            _member_response(record)
            for record in get_workspace_members(database_session, context)
        ],
    )


@router.patch(
    "/{workspace_id}/members/{membership_id}/role",
    response_model=RoleAssignmentResponse,
)
def update_member_role(
    membership_id: uuid.UUID,
    payload: RoleAssignmentRequest,
    context: Annotated[WorkspaceContext, Depends(owner_workspace_access)],
    database_session: Annotated[Session, Depends(get_db)],
) -> RoleAssignmentResponse:
    try:
        member = assign_member_role(
            database_session,
            context,
            membership_id,
            payload.role,
        )
    except WorkspaceMemberNotFoundError as exception:
        raise HTTPException(status_code=404, detail="Workspace member not found") from exception
    except LastActiveOwnerError as exception:
        raise HTTPException(
            status_code=409,
            detail="The last active owner cannot be removed",
        ) from exception
    return RoleAssignmentResponse(
        success=True,
        message="Member role updated",
        data=_member_response(member),
    )


@router.delete(
    "/{workspace_id}/members/{membership_id}",
    response_model=MembershipActionResponse,
)
def remove_member(
    membership_id: uuid.UUID,
    context: Annotated[WorkspaceContext, Depends(owner_workspace_access)],
    database_session: Annotated[Session, Depends(get_db)],
) -> MembershipActionResponse:
    try:
        revoke_member(database_session, context, membership_id)
    except WorkspaceMemberNotFoundError as exception:
        raise HTTPException(status_code=404, detail="Workspace member not found") from exception
    except LastActiveOwnerError as exception:
        raise HTTPException(
            status_code=409,
            detail="The last active owner cannot be removed",
        ) from exception
    return MembershipActionResponse(success=True, message="Member access revoked")


@router.post(
    "/{workspace_id}/members/{membership_id}/integration-access",
    response_model=PermissionDelegationResponse,
    status_code=status.HTTP_200_OK,
)
def grant_integration_access(
    membership_id: uuid.UUID,
    context: Annotated[WorkspaceContext, Depends(owner_workspace_access)],
    database_session: Annotated[Session, Depends(get_db)],
) -> PermissionDelegationResponse:
    try:
        delegate_operator_integration_access(
            database_session,
            context,
            membership_id,
        )
    except InvalidPermissionDelegationError as exception:
        raise HTTPException(
            status_code=400,
            detail="Integration access can only be delegated to an active operator",
        ) from exception
    return PermissionDelegationResponse(
        success=True,
        message="Integration access delegated",
    )
