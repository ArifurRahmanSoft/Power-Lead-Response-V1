from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import AuthenticatedIdentity, get_authenticated_identity
from app.schemas.auth import (
    AuthSessionData,
    AuthenticationErrorResponse,
    LoginData,
    LoginRequest,
    LoginResponse,
    LoginUser,
    MeResponse,
    SelectedWorkspace,
    WorkspaceChoice,
    WorkspaceSelectionRequest,
)
from app.schemas.password_reset import (
    ForgotPasswordRequest,
    PasswordResetErrorResponse,
    PasswordResetResponse,
    ResetPasswordRequest,
)
from app.schemas.user import ErrorResponse, RegisteredUser, RegistrationResponse, UserRegister
from app.services.auth_service import (
    AuthenticationResult,
    InactiveWorkspaceAccessError,
    InvalidCredentialsError,
    InvalidWorkspaceSelectionError,
    authenticate_user,
    get_current_session,
    select_workspace,
)
from app.services.password_reset_service import (
    InvalidPasswordResetTokenError,
    request_password_reset,
    reset_password,
)
from app.services.user_service import (
    DuplicateEmailError,
    DuplicateLoginIdError,
    register_user,
)

router = APIRouter()

FORGOT_PASSWORD_MESSAGE = (
    "If the email exists, a password reset link has been sent."
)


def _session_data(result: AuthenticationResult) -> AuthSessionData:
    selected = result.selected_membership
    return AuthSessionData(
        auth_status=result.auth_status,
        user=LoginUser(
            id=result.user.id,
            login_id=result.user.login_id,
            name=result.user.name,
            email=result.user.email,
        ),
        selected_workspace=(
            SelectedWorkspace(
                id=selected.tenant.id,
                name=selected.tenant.name,
                slug=selected.tenant.slug,
            )
            if selected is not None
            else None
        ),
        role=selected.role.key if selected is not None else None,
        permissions=list(result.permissions),
        workspaces=[
            WorkspaceChoice(
                id=record.tenant.id,
                name=record.tenant.name,
                slug=record.tenant.slug,
                role=record.role.key,
            )
            for record in result.memberships
        ],
    )


def _login_response(result: AuthenticationResult) -> LoginResponse:
    if result.access_token is None:
        raise RuntimeError("Authentication result did not contain an access token")
    session_data = _session_data(result)
    return LoginResponse(
        success=True,
        message="Login successful",
        data=LoginData(
            **session_data.model_dump(),
            access_token=result.access_token,
            token_type="bearer",
        ),
    )


@router.post(
    "/forgot-password",
    response_model=PasswordResetResponse,
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse}},
)
def forgot_password(
    payload: ForgotPasswordRequest,
    database_session: Annotated[Session, Depends(get_db)],
) -> PasswordResetResponse:
    request_password_reset(database_session, payload)
    return PasswordResetResponse(success=True, message=FORGOT_PASSWORD_MESSAGE)


@router.post(
    "/reset-password",
    response_model=PasswordResetResponse,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": PasswordResetErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    },
)
def perform_password_reset(
    payload: ResetPasswordRequest,
    database_session: Annotated[Session, Depends(get_db)],
) -> PasswordResetResponse | JSONResponse:
    try:
        reset_password(database_session, payload)
    except InvalidPasswordResetTokenError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "success": False,
                "message": "Invalid or expired password reset token",
            },
        )
    return PasswordResetResponse(success=True, message="Password reset successful")


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": AuthenticationErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    },
)
def login(
    payload: LoginRequest,
    database_session: Annotated[Session, Depends(get_db)],
) -> LoginResponse | JSONResponse:
    try:
        result = authenticate_user(database_session, payload)
    except InvalidCredentialsError:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
            content={
                "success": False,
                "message": "Invalid identifier or password",
            },
        )
    return _login_response(result)


@router.get("/me", response_model=MeResponse)
def me(
    identity: Annotated[AuthenticatedIdentity, Depends(get_authenticated_identity)],
    database_session: Annotated[Session, Depends(get_db)],
) -> MeResponse:
    try:
        result = get_current_session(database_session, identity)
    except InactiveWorkspaceAccessError as exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace access is no longer active",
        ) from exception
    return MeResponse(success=True, data=_session_data(result))


@router.post("/select-workspace", response_model=LoginResponse)
def choose_workspace(
    payload: WorkspaceSelectionRequest,
    identity: Annotated[AuthenticatedIdentity, Depends(get_authenticated_identity)],
    database_session: Annotated[Session, Depends(get_db)],
) -> LoginResponse:
    try:
        result = select_workspace(
            database_session,
            identity.user,
            payload.workspace_id,
        )
    except InvalidWorkspaceSelectionError as exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace selection is not authorized",
        ) from exception
    return _login_response(result)


@router.post(
    "/register",
    response_model=RegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    },
)
def register(
    payload: UserRegister,
    database_session: Annotated[Session, Depends(get_db)],
) -> RegistrationResponse | JSONResponse:
    try:
        user = register_user(database_session, payload)
    except DuplicateEmailError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"success": False, "message": "Email already registered"},
        )
    except DuplicateLoginIdError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"success": False, "message": "Login ID already registered"},
        )

    return RegistrationResponse(
        success=True,
        message="Registration successful",
        data=RegisteredUser(
            id=user.id,
            name=user.name,
            email=user.email,
            login_id=user.login_id,
        ),
    )
