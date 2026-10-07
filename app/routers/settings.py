import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.models.role import Role
from app.repositories.settings_repository import SettingsUserRecord
from app.schemas.menu_permission import (
    MenuCatalogItem,
    MenuCatalogResponse,
    RolePermissionData,
    RolePermissionResponse,
    RolePermissionUpdateRequest,
    RolePermissionUpdateResponse,
)
from app.schemas.settings_role import (
    RoleCreateRequest,
    RoleData,
    RoleDeleteResponse,
    RoleListData,
    RoleListResponse,
    RoleResponse,
    RoleUpdateRequest,
)
from app.schemas.settings_user import (
    AdminPasswordResetRequest,
    UserSetupActionResponse,
    UserSetupCreateRequest,
    UserSetupData,
    UserSetupListData,
    UserSetupListResponse,
    UserSetupResponse,
    UserSetupUpdateRequest,
)
from app.services.menu_permission_service import (
    InvalidPermissionSetError,
    MenuRoleNotFoundError,
    PermissionEscalationError,
    get_menu_catalog,
    get_menu_permissions_for_role,
    update_menu_permissions_for_role,
)
from app.services.settings_role_service import (
    AssignedRoleConflictError,
    DuplicateRoleNameError,
    SettingsRoleNotFoundError,
    SystemRoleImmutableError,
    create_role,
    delete_role,
    get_roles,
    update_role,
)
from app.services.settings_user_service import (
    DuplicateSettingsUserError,
    InvalidSettingsRoleError,
    LastSettingsOwnerError,
    MultiTenantAccountLimitationError,
    SelfEscalationError,
    SettingsUserNotFoundError,
    create_settings_user,
    get_settings_users,
    reset_settings_user_password,
    revoke_settings_user,
    update_settings_user,
)

router = APIRouter()


def _role_data(role: Role) -> RoleData:
    return RoleData(id=role.id, key=role.key, name=role.name, is_system=role.is_system)


def _user_data(record: SettingsUserRecord) -> UserSetupData:
    return UserSetupData(
        id=record.user.id,
        membership_id=record.membership.id,
        display_name=record.user.name,
        email=record.user.email,
        login_id=record.user.login_id,
        role_id=record.role.id,
        role_name=record.role.name,
        is_active=record.membership.is_active and record.membership.revoked_at is None,
    )


def _raise_user_service_error(exception: Exception) -> None:
    if isinstance(exception, SettingsUserNotFoundError):
        raise HTTPException(status_code=404, detail="Tenant user not found") from exception
    if isinstance(exception, DuplicateSettingsUserError):
        raise HTTPException(status_code=409, detail="Email or login ID already registered") from exception
    if isinstance(exception, InvalidSettingsRoleError):
        raise HTTPException(status_code=400, detail="Role is not available in this tenant") from exception
    if isinstance(exception, MultiTenantAccountLimitationError):
        raise HTTPException(
            status_code=409,
            detail=(
                "This global account belongs to multiple tenants; use the separate "
                "account-management flow"
            ),
        ) from exception
    if isinstance(exception, LastSettingsOwnerError):
        raise HTTPException(status_code=409, detail="The last active owner cannot be removed") from exception
    if isinstance(exception, (PermissionEscalationError, SelfEscalationError)):
        raise HTTPException(status_code=403, detail="Role assignment exceeds your authorization") from exception
    raise exception


@router.get("/roles", response_model=RoleListResponse)
def list_role_settings(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_ROLES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RoleListResponse:
    result = get_roles(database_session, context, page, size)
    return RoleListResponse(
        success=True,
        data=RoleListData(
            items=[_role_data(role) for role in result.items],
            page=page,
            size=size,
            total=result.total,
        ),
    )


@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
def create_role_setting(
    payload: RoleCreateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_ROLES_CREATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> RoleResponse:
    try:
        role = create_role(database_session, context, payload.name)
    except DuplicateRoleNameError as exception:
        raise HTTPException(status_code=409, detail="Role name already exists") from exception
    return RoleResponse(success=True, message="Role created", data=_role_data(role))


@router.patch("/roles/{role_id}", response_model=RoleResponse)
def update_role_setting(
    role_id: uuid.UUID,
    payload: RoleUpdateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_ROLES_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> RoleResponse:
    try:
        role = update_role(database_session, context, role_id, payload.name)
    except SettingsRoleNotFoundError as exception:
        raise HTTPException(status_code=404, detail="Role not found") from exception
    except DuplicateRoleNameError as exception:
        raise HTTPException(status_code=409, detail="Role name already exists") from exception
    except SystemRoleImmutableError as exception:
        raise HTTPException(status_code=409, detail="System roles cannot be renamed") from exception
    return RoleResponse(success=True, message="Role updated", data=_role_data(role))


@router.delete("/roles/{role_id}", response_model=RoleDeleteResponse)
def delete_role_setting(
    role_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_ROLES_DELETE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> RoleDeleteResponse:
    try:
        delete_role(database_session, context, role_id)
    except SettingsRoleNotFoundError as exception:
        raise HTTPException(status_code=404, detail="Role not found") from exception
    except SystemRoleImmutableError as exception:
        raise HTTPException(status_code=409, detail="System roles cannot be deleted") from exception
    except AssignedRoleConflictError as exception:
        raise HTTPException(status_code=409, detail="Role is assigned to tenant users") from exception
    return RoleDeleteResponse(success=True, message="Role deleted")


@router.get("/menu-catalog", response_model=MenuCatalogResponse)
def menu_catalog(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_MENU_PERMISSIONS_VIEW)),
    ],
) -> MenuCatalogResponse:
    del context
    return MenuCatalogResponse(
        success=True,
        data=[
            MenuCatalogItem(
                key=menu.key,
                label=menu.label,
                actions=list(menu.actions),
                permission_keys=[f"{menu.key}.{action}" for action in menu.actions],
            )
            for menu in get_menu_catalog()
        ],
    )


@router.get("/roles/{role_id}/permissions", response_model=RolePermissionResponse)
def role_permissions(
    role_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_MENU_PERMISSIONS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> RolePermissionResponse:
    try:
        role, keys = get_menu_permissions_for_role(database_session, context, role_id)
    except MenuRoleNotFoundError as exception:
        raise HTTPException(status_code=404, detail="Role not found") from exception
    return RolePermissionResponse(
        success=True,
        data=RolePermissionData(role_id=role.id, role_name=role.name, permission_keys=keys),
    )


@router.put("/roles/{role_id}/permissions", response_model=RolePermissionUpdateResponse)
def update_role_permissions(
    role_id: uuid.UUID,
    payload: RolePermissionUpdateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_MENU_PERMISSIONS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> RolePermissionUpdateResponse:
    try:
        role, keys = update_menu_permissions_for_role(
            database_session,
            context,
            role_id,
            payload.permission_keys,
        )
    except MenuRoleNotFoundError as exception:
        raise HTTPException(status_code=404, detail="Role not found") from exception
    except InvalidPermissionSetError as exception:
        raise HTTPException(status_code=400, detail="Invalid menu permission combination") from exception
    except PermissionEscalationError as exception:
        raise HTTPException(status_code=403, detail="Permission update exceeds your authorization") from exception
    return RolePermissionUpdateResponse(
        success=True,
        message="Role permissions updated",
        data=RolePermissionData(role_id=role.id, role_name=role.name, permission_keys=keys),
    )


@router.get("/users", response_model=UserSetupListResponse)
def list_user_settings(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_USERS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> UserSetupListResponse:
    result = get_settings_users(database_session, context, page, size)
    return UserSetupListResponse(
        success=True,
        data=UserSetupListData(
            items=[_user_data(record) for record in result.items],
            page=page,
            size=size,
            total=result.total,
        ),
    )


@router.post("/users", response_model=UserSetupResponse, status_code=status.HTTP_201_CREATED)
def create_user_setting(
    payload: UserSetupCreateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_USERS_CREATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> UserSetupResponse:
    try:
        record = create_settings_user(database_session, context, payload)
    except Exception as exception:
        _raise_user_service_error(exception)
        raise
    return UserSetupResponse(success=True, message="Tenant user created", data=_user_data(record))


@router.patch("/users/{user_id}", response_model=UserSetupResponse)
def update_user_setting(
    user_id: uuid.UUID,
    payload: UserSetupUpdateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_USERS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> UserSetupResponse:
    try:
        record = update_settings_user(database_session, context, user_id, payload)
    except Exception as exception:
        _raise_user_service_error(exception)
        raise
    return UserSetupResponse(success=True, message="Tenant user updated", data=_user_data(record))


@router.delete("/users/{user_id}", response_model=UserSetupActionResponse)
def delete_user_setting(
    user_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_USERS_DELETE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> UserSetupActionResponse:
    try:
        revoke_settings_user(database_session, context, user_id)
    except Exception as exception:
        _raise_user_service_error(exception)
        raise
    return UserSetupActionResponse(success=True, message="Tenant access revoked")


@router.post("/users/{user_id}/reset-password", response_model=UserSetupActionResponse)
def reset_user_password(
    user_id: uuid.UUID,
    payload: AdminPasswordResetRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.SETTINGS_USERS_RESET_PASSWORD)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> UserSetupActionResponse:
    try:
        reset_settings_user_password(database_session, context, user_id, payload)
    except Exception as exception:
        _raise_user_service_error(exception)
        raise
    return UserSetupActionResponse(success=True, message="Password reset successful")
