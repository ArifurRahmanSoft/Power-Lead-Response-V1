import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.rate_limit import RateLimitExceededError
from app.core.security import WorkspaceContext, require_permission
from app.models.sender_account import SenderAccount
from app.providers.smtp import SmtpProvider, SmtpProviderError, get_smtp_provider
from app.schemas.sender_account import (
    SenderAccountCreateRequest,
    SenderAccountData,
    SenderAccountListData,
    SenderAccountListResponse,
    SenderAccountResponse,
    SenderAccountUpdateRequest,
    SenderActionResponse,
    SenderConnectionTestData,
    SenderConnectionTestResponse,
    SenderTestEmailRequest,
)
from app.services.sender_account_service import (
    DuplicateSenderEmailError,
    InactiveSenderAccountError,
    InvalidSenderConfigurationError,
    SenderAccountNotFoundError,
    create_sender_account,
    deactivate_sender_account,
    delete_sender_account,
    get_sender_accounts,
    get_sender_for_context,
    send_sender_test_email,
    set_default_sender_account,
    test_sender_connection,
    update_sender_account,
)
from app.utils.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryptionNotConfiguredError,
)

router = APIRouter()


def _data(account: SenderAccount) -> SenderAccountData:
    return SenderAccountData(
        id=account.id,
        display_name=account.display_name,
        sender_email=account.sender_email,
        provider=account.provider,
        authentication_type=account.authentication_type,
        smtp_host=account.smtp_host,
        smtp_port=account.smtp_port,
        encryption=account.encryption,
        smtp_username=account.smtp_username,
        credentials_configured=bool(account.credential_ciphertext),
        reply_to=account.reply_to,
        is_active=account.is_active,
        is_default=account.is_default,
        connection_status=account.connection_status,
        last_tested_at=account.last_tested_at,
        last_test_message=account.last_test_message,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def _raise_service_error(exception: Exception) -> None:
    if isinstance(exception, SenderAccountNotFoundError):
        raise HTTPException(status_code=404, detail="Sender account not found") from exception
    if isinstance(exception, DuplicateSenderEmailError):
        raise HTTPException(
            status_code=409,
            detail="A sender account with this email already exists in the workspace",
        ) from exception
    if isinstance(exception, InactiveSenderAccountError):
        raise HTTPException(status_code=409, detail="Sender account is inactive") from exception
    if isinstance(exception, InvalidSenderConfigurationError):
        raise HTTPException(status_code=400, detail="Invalid SMTP provider configuration") from exception
    if isinstance(
        exception,
        (CredentialEncryptionNotConfiguredError, CredentialDecryptionError),
    ):
        raise HTTPException(
            status_code=503,
            detail="SMTP credential encryption is not configured",
        ) from exception
    raise exception


@router.get("/sender-accounts", response_model=SenderAccountListResponse)
def list_senders(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    include_inactive: bool = True,
) -> SenderAccountListResponse:
    result = get_sender_accounts(
        database_session,
        context,
        page,
        size,
        include_inactive=include_inactive,
    )
    return SenderAccountListResponse(
        success=True,
        data=SenderAccountListData(
            items=[_data(account) for account in result.items],
            page=page,
            size=size,
            total=result.total,
        ),
    )


@router.post(
    "/sender-accounts",
    response_model=SenderAccountResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_sender(
    payload: SenderAccountCreateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> SenderAccountResponse:
    try:
        account = create_sender_account(database_session, context, payload)
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return SenderAccountResponse(
        success=True,
        message="Sender account created",
        data=_data(account),
    )


@router.get("/sender-accounts/{sender_account_id}", response_model=SenderAccountResponse)
def get_sender(
    sender_account_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> SenderAccountResponse:
    try:
        account = get_sender_for_context(database_session, context, sender_account_id)
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return SenderAccountResponse(success=True, data=_data(account))


@router.patch("/sender-accounts/{sender_account_id}", response_model=SenderAccountResponse)
def update_sender(
    sender_account_id: uuid.UUID,
    payload: SenderAccountUpdateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> SenderAccountResponse:
    try:
        account = update_sender_account(
            database_session,
            context,
            sender_account_id,
            payload,
        )
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return SenderAccountResponse(
        success=True,
        message="Sender account updated",
        data=_data(account),
    )


@router.post(
    "/sender-accounts/{sender_account_id}/deactivate",
    response_model=SenderAccountResponse,
)
def deactivate_sender(
    sender_account_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> SenderAccountResponse:
    try:
        account = deactivate_sender_account(
            database_session,
            context,
            sender_account_id,
        )
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return SenderAccountResponse(
        success=True,
        message="Sender account deactivated",
        data=_data(account),
    )


@router.delete(
    "/sender-accounts/{sender_account_id}",
    response_model=SenderActionResponse,
)
def delete_sender(
    sender_account_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> SenderActionResponse:
    try:
        preserved = delete_sender_account(
            database_session,
            context,
            sender_account_id,
        )
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return SenderActionResponse(
        success=True,
        message=(
            "Sender account is referenced by message history and was deactivated"
            if preserved
            else "Sender account deleted"
        ),
    )


@router.post(
    "/sender-accounts/{sender_account_id}/set-default",
    response_model=SenderAccountResponse,
)
def set_default_sender(
    sender_account_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> SenderAccountResponse:
    try:
        account = set_default_sender_account(
            database_session,
            context,
            sender_account_id,
        )
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return SenderAccountResponse(
        success=True,
        message="Default sender updated",
        data=_data(account),
    )


@router.post(
    "/sender-accounts/{sender_account_id}/test-connection",
    response_model=SenderConnectionTestResponse,
)
async def test_sender(
    sender_account_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEST)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[SmtpProvider, Depends(get_smtp_provider)],
) -> SenderConnectionTestResponse:
    try:
        account, tested_at = await test_sender_connection(
            database_session,
            context,
            sender_account_id,
            provider,
        )
    except SmtpProviderError as exception:
        return SenderConnectionTestResponse(
            success=False,
            message=exception.safe_message,
            data=SenderConnectionTestData(
                sender_account_id=sender_account_id,
                connection_status="failed",
                last_tested_at=exception.tested_at,
            ),
        )
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return SenderConnectionTestResponse(
        success=True,
        message="SMTP authentication successful; no email was sent",
        data=SenderConnectionTestData(
            sender_account_id=account.id,
            connection_status="success",
            last_tested_at=tested_at,
        ),
    )


@router.post(
    "/sender-accounts/{sender_account_id}/test-email",
    response_model=SenderActionResponse,
)
async def send_test_email(
    sender_account_id: uuid.UUID,
    payload: SenderTestEmailRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEST)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    provider: Annotated[SmtpProvider, Depends(get_smtp_provider)],
) -> SenderActionResponse:
    try:
        await send_sender_test_email(
            database_session,
            context,
            sender_account_id,
            str(payload.recipient),
            provider,
        )
    except RateLimitExceededError as exception:
        raise HTTPException(
            status_code=429,
            detail="Too many SMTP test emails; try again later",
        ) from exception
    except SmtpProviderError as exception:
        raise HTTPException(status_code=502, detail=exception.safe_message) from exception
    except Exception as exception:
        _raise_service_error(exception)
        raise
    return SenderActionResponse(success=True, message="Test email sent")
