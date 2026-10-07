import uuid

from pydantic import BaseModel

from app.core.permissions import RoleKey


class WorkspaceMember(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    login_id: str
    name: str
    email: str
    role: RoleKey
    is_active: bool


class WorkspaceMemberListResponse(BaseModel):
    success: bool
    data: list[WorkspaceMember]


class RoleAssignmentRequest(BaseModel):
    role: RoleKey


class RoleAssignmentResponse(BaseModel):
    success: bool
    message: str
    data: WorkspaceMember


class MembershipActionResponse(BaseModel):
    success: bool
    message: str


class PermissionDelegationResponse(BaseModel):
    success: bool
    message: str
