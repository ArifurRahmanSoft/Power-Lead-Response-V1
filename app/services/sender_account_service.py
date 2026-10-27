import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.rate_limit import smtp_test_email_rate_limiter
from app.core.security import WorkspaceContext
from app.models.sender_account import SenderAccount
from app.providers.smtp import SmtpDeliveryConfig, SmtpProvider, SmtpProviderError
from app.repositories.sender_account_repository import (
    PaginatedSenderAccounts,
    clear_default_sender,
    get_sender_account,
    list_sender_accounts,
    sender_account_is_referenced_by_message_history,
    sender_email_exists,
)
from app.repositories.workspace_repository import add_authorization_audit, lock_tenant
from app.schemas.sender_account import (
    SenderAccountCreateRequest,
    SenderAccountUpdateRequest,
)
from app.utils.credential_encryption import decrypt_credential, encrypt_credential


class SenderAccountNotFoundError(Exception):
    pass


class DuplicateSenderEmailError(Exception):
    pass


class InactiveSenderAccountError(Exception):
    pass


class InvalidSenderConfigurationError(Exception):
    pass


def get_sender_accounts(
    database_session: Session,
    context: WorkspaceContext,
    page: int,
    size: int,
    *,
    include_inactive: bool,
) -> PaginatedSenderAccounts:
    return list_sender_accounts(
        database_session,
        context.tenant.id,
        offset=(page - 1) * size,
        limit=size,
        include_inactive=include_inactive,
    )


def get_sender_for_context(
    database_session: Session,
    context: WorkspaceContext,
    sender_account_id: uuid.UUID,
) -> SenderAccount:
    account = get_sender_account(
        database_session,
        context.tenant.id,
        sender_account_id,
    )
    if account is None:
        raise SenderAccountNotFoundError
    return account


def _audit(
    database_session: Session,
    context: WorkspaceContext,
    action: str,
    account_id: uuid.UUID,
    details: dict[str, object] | None = None,
) -> None:
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action=action,
        target_membership_id=None,
        details={"sender_account_id": str(account_id), **(details or {})},
    )


def create_sender_account(
    database_session: Session,
    context: WorkspaceContext,
    payload: SenderAccountCreateRequest,
) -> SenderAccount:
    lock_tenant(database_session, context.tenant.id)
    sender_email = str(payload.sender_email).lower()
    if sender_email_exists(database_session, context.tenant.id, sender_email):
        database_session.rollback()
        raise DuplicateSenderEmailError
    if payload.is_default:
        clear_default_sender(database_session, context.tenant.id)

    account = SenderAccount(
        tenant_id=context.tenant.id,
        display_name=payload.display_name,
        sender_email=sender_email,
        provider=payload.provider,
        authentication_type=payload.authentication_type,
        smtp_host=str(payload.smtp_host),
        smtp_port=int(payload.smtp_port),
        encryption=payload.encryption,
        smtp_username=str(payload.smtp_username),
        credential_ciphertext=encrypt_credential(payload.credential.get_secret_value()),
        reply_to=str(payload.reply_to).lower() if payload.reply_to else None,
        is_active=payload.is_active,
        is_default=payload.is_default,
        created_by_user_id=context.user.id,
        updated_by_user_id=context.user.id,
    )
    database_session.add(account)
    database_session.flush()
    _audit(database_session, context, "sender_account.created", account.id)
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        raise DuplicateSenderEmailError from exception
    database_session.refresh(account)
    return account


def _merged_update_configuration(
    account: SenderAccount,
    payload: SenderAccountUpdateRequest,
) -> dict[str, object]:
    changes = payload.model_dump(exclude_unset=True, exclude={"credential"})
    current: dict[str, object] = {
        "display_name": account.display_name,
        "sender_email": account.sender_email,
        "provider": account.provider,
        "authentication_type": account.authentication_type,
        "smtp_host": account.smtp_host,
        "smtp_port": account.smtp_port,
        "encryption": account.encryption,
        "smtp_username": account.smtp_username,
        "reply_to": account.reply_to,
        "is_active": account.is_active,
    }
    previous_provider = account.provider
    previous_email = account.sender_email
    current.update(changes)

    if current["provider"] == "gmail":
        if previous_provider != "gmail":
            current["authentication_type"] = changes.get(
                "authentication_type", "app_password"
            )
            current["smtp_host"] = changes.get("smtp_host", "smtp.gmail.com")
            current["smtp_port"] = changes.get(
                "smtp_port", 465 if current["encryption"] == "TLS" else 587
            )
            current["smtp_username"] = changes.get(
                "smtp_username", current["sender_email"]
            )
        elif (
            "sender_email" in changes
            and "smtp_username" not in changes
            and account.smtp_username == previous_email
        ):
            current["smtp_username"] = current["sender_email"]
        if current["authentication_type"] != "app_password":
            raise InvalidSenderConfigurationError
        if current["smtp_host"] != "smtp.gmail.com":
            raise InvalidSenderConfigurationError
    else:
        if previous_provider != "custom_smtp":
            current["authentication_type"] = changes.get(
                "authentication_type", "password"
            )
        if current["authentication_type"] != "password" or any(
            current[field] in (None, "")
            for field in ("smtp_host", "smtp_port", "smtp_username")
        ):
            raise InvalidSenderConfigurationError
    return current


def update_sender_account(
    database_session: Session,
    context: WorkspaceContext,
    sender_account_id: uuid.UUID,
    payload: SenderAccountUpdateRequest,
) -> SenderAccount:
    lock_tenant(database_session, context.tenant.id)
    account = get_sender_account(
        database_session,
        context.tenant.id,
        sender_account_id,
        for_update=True,
    )
    if account is None:
        database_session.rollback()
        raise SenderAccountNotFoundError
    values = _merged_update_configuration(account, payload)
    sender_email = str(values["sender_email"]).lower()
    if sender_email_exists(
        database_session,
        context.tenant.id,
        sender_email,
        exclude_id=account.id,
    ):
        database_session.rollback()
        raise DuplicateSenderEmailError

    changed_fields = sorted(payload.model_fields_set - {"credential"})
    transport_fields = {
        "sender_email",
        "provider",
        "authentication_type",
        "smtp_host",
        "smtp_port",
        "encryption",
        "smtp_username",
    }
    for field, value in values.items():
        if field == "sender_email":
            value = sender_email
        elif field == "reply_to" and value is not None:
            value = str(value).lower()
        setattr(account, field, value)
    if payload.credential is not None:
        account.credential_ciphertext = encrypt_credential(
            payload.credential.get_secret_value()
        )
        transport_fields.add("credential")
    if account.is_default and not account.is_active:
        account.is_default = False
    if transport_fields & payload.model_fields_set:
        account.connection_status = "not_tested"
        account.last_tested_at = None
        account.last_test_message = None
    account.updated_by_user_id = context.user.id
    _audit(
        database_session,
        context,
        "sender_account.updated",
        account.id,
        {
            "changed_fields": changed_fields,
            "credential_updated": payload.credential is not None,
        },
    )
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        raise DuplicateSenderEmailError from exception
    database_session.refresh(account)
    return account


def deactivate_sender_account(
    database_session: Session,
    context: WorkspaceContext,
    sender_account_id: uuid.UUID,
) -> SenderAccount:
    lock_tenant(database_session, context.tenant.id)
    account = get_sender_account(
        database_session,
        context.tenant.id,
        sender_account_id,
        for_update=True,
    )
    if account is None:
        database_session.rollback()
        raise SenderAccountNotFoundError
    account.is_active = False
    account.is_default = False
    account.updated_by_user_id = context.user.id
    _audit(database_session, context, "sender_account.deactivated", account.id)
    database_session.commit()
    database_session.refresh(account)
    return account


def delete_sender_account(
    database_session: Session,
    context: WorkspaceContext,
    sender_account_id: uuid.UUID,
) -> bool:
    """Return True when history required deactivation instead of deletion."""
    lock_tenant(database_session, context.tenant.id)
    account = get_sender_account(
        database_session,
        context.tenant.id,
        sender_account_id,
        for_update=True,
    )
    if account is None:
        database_session.rollback()
        raise SenderAccountNotFoundError
    if sender_account_is_referenced_by_message_history(database_session, account.id):
        account.is_active = False
        account.is_default = False
        account.updated_by_user_id = context.user.id
        _audit(
            database_session,
            context,
            "sender_account.deactivated_for_history",
            account.id,
        )
        database_session.commit()
        return True

    _audit(database_session, context, "sender_account.deleted", account.id)
    database_session.delete(account)
    database_session.commit()
    return False


def set_default_sender_account(
    database_session: Session,
    context: WorkspaceContext,
    sender_account_id: uuid.UUID,
) -> SenderAccount:
    lock_tenant(database_session, context.tenant.id)
    account = get_sender_account(
        database_session,
        context.tenant.id,
        sender_account_id,
        for_update=True,
    )
    if account is None:
        database_session.rollback()
        raise SenderAccountNotFoundError
    if not account.is_active:
        database_session.rollback()
        raise InactiveSenderAccountError
    clear_default_sender(
        database_session,
        context.tenant.id,
        exclude_id=account.id,
    )
    account.is_default = True
    account.updated_by_user_id = context.user.id
    _audit(database_session, context, "sender_account.default_set", account.id)
    database_session.commit()
    database_session.refresh(account)
    return account


def sender_delivery_config(account: SenderAccount) -> SmtpDeliveryConfig:
    return SmtpDeliveryConfig(
        display_name=account.display_name,
        sender_email=account.sender_email,
        smtp_host=account.smtp_host,
        smtp_port=account.smtp_port,
        encryption=account.encryption,
        smtp_username=account.smtp_username,
        credential=decrypt_credential(account.credential_ciphertext),
        reply_to=account.reply_to,
    )


def _record_test_result(
    database_session: Session,
    context: WorkspaceContext,
    account: SenderAccount,
    *,
    succeeded: bool,
    result_code: str,
) -> datetime:
    tested_at = datetime.now(timezone.utc)
    account.connection_status = "success" if succeeded else "failed"
    account.last_tested_at = tested_at
    account.last_test_message = (
        "SMTP authentication successful"
        if succeeded
        else "SMTP authentication or connection failed"
    )
    account.updated_by_user_id = context.user.id
    _audit(
        database_session,
        context,
        "sender_account.connection_tested",
        account.id,
        {"result": "success" if succeeded else "failed", "code": result_code},
    )
    database_session.commit()
    return tested_at


async def test_sender_connection(
    database_session: Session,
    context: WorkspaceContext,
    sender_account_id: uuid.UUID,
    provider: SmtpProvider,
) -> tuple[SenderAccount, datetime]:
    account = get_sender_for_context(database_session, context, sender_account_id)
    if not account.is_active:
        raise InactiveSenderAccountError
    try:
        await provider.test_connection(sender_delivery_config(account))
    except SmtpProviderError as exception:
        tested_at = _record_test_result(
            database_session,
            context,
            account,
            succeeded=False,
            result_code=exception.code,
        )
        exception.tested_at = tested_at
        raise
    tested_at = _record_test_result(
        database_session,
        context,
        account,
        succeeded=True,
        result_code="authenticated",
    )
    database_session.refresh(account)
    return account, tested_at


async def send_sender_test_email(
    database_session: Session,
    context: WorkspaceContext,
    sender_account_id: uuid.UUID,
    recipient: str,
    provider: SmtpProvider,
) -> datetime:
    await smtp_test_email_rate_limiter.enforce(
        f"{context.tenant.id}:{context.user.id}:{sender_account_id}",
        limit=settings.smtp_test_email_rate_limit_requests,
        window_seconds=settings.smtp_test_email_rate_limit_window_seconds,
    )
    account = get_sender_for_context(database_session, context, sender_account_id)
    if not account.is_active:
        raise InactiveSenderAccountError
    try:
        await provider.send_test_email(sender_delivery_config(account), recipient)
    except SmtpProviderError as exception:
        _record_test_result(
            database_session,
            context,
            account,
            succeeded=False,
            result_code=exception.code,
        )
        raise
    tested_at = _record_test_result(
        database_session,
        context,
        account,
        succeeded=True,
        result_code="test_email_sent",
    )
    _audit(
        database_session,
        context,
        "sender_account.test_email_sent",
        account.id,
    )
    database_session.commit()
    return tested_at
