import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import WorkspaceContext
from app.models.meta_whatsapp_oauth import MetaWhatsAppOAuthOperation
from app.models.whatsapp import WhatsAppChannel
from app.providers.meta_whatsapp import (
    MetaWhatsAppConfigurationError,
    MetaWhatsAppError,
    MetaWhatsAppProvider,
)
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.meta_whatsapp import MetaWhatsAppCompleteRequest
from app.utils.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryptionNotConfiguredError,
    decrypt_credential,
    encrypt_credential,
)


class MetaWhatsAppOAuthStateError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class MetaWhatsAppAssetConflictError(Exception):
    code = "meta_whatsapp_asset_conflict"
    message = "The authorized WhatsApp account is already mapped to another workspace"


@dataclass(frozen=True)
class MetaWhatsAppOAuthStart:
    operation: MetaWhatsAppOAuthOperation
    state: str
    launch: dict[str, object]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _state_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def begin_meta_whatsapp_oauth(
    database_session: Session,
    context: WorkspaceContext,
    provider: MetaWhatsAppProvider,
    *,
    expire_minutes: int,
) -> MetaWhatsAppOAuthStart:
    launch = provider.embedded_signup_options()
    state = secrets.token_urlsafe(32)
    operation = MetaWhatsAppOAuthOperation(
        tenant_id=context.tenant.id,
        requested_by_user_id=context.user.id,
        state_hash=_state_hash(state),
        configuration_id=provider.embedded_signup_config_id,
        redirect_uri=provider.oauth_redirect_uri,
        status="pending",
        expires_at=_now() + timedelta(minutes=expire_minutes),
    )
    database_session.add(operation)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="meta_whatsapp.oauth.started",
        target_membership_id=context.membership.id,
        details={"operation_id": str(operation.id), "mode": "coexistence"},
    )
    database_session.commit()
    database_session.refresh(operation)
    return MetaWhatsAppOAuthStart(operation=operation, state=state, launch=launch)


def get_meta_whatsapp_oauth_operation(
    database_session: Session,
    context: WorkspaceContext,
    operation_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> MetaWhatsAppOAuthOperation:
    statement = select(MetaWhatsAppOAuthOperation).where(
        MetaWhatsAppOAuthOperation.id == operation_id,
        MetaWhatsAppOAuthOperation.tenant_id == context.tenant.id,
    )
    if for_update:
        statement = statement.with_for_update()
    operation = database_session.scalar(statement)
    if operation is None:
        raise MetaWhatsAppOAuthStateError(
            "oauth_operation_not_found", "WhatsApp onboarding operation not found"
        )
    return operation


def _record_failure(
    database_session: Session,
    operation: MetaWhatsAppOAuthOperation,
    *,
    status: str = "failed",
    reason_code: str,
    provider_http_status: int | None = None,
) -> None:
    operation.status = status
    operation.reason_code = reason_code
    operation.provider_http_status = provider_http_status
    add_authorization_audit(
        database_session,
        tenant_id=operation.tenant_id,
        actor_user_id=operation.requested_by_user_id,
        action="meta_whatsapp.oauth.failed",
        target_membership_id=None,
        details={
            "operation_id": str(operation.id),
            "reason_code": reason_code,
            "provider_http_status": provider_http_status,
        },
    )
    database_session.commit()


def cancel_meta_whatsapp_oauth(
    database_session: Session,
    context: WorkspaceContext,
    *,
    operation_id: uuid.UUID,
    state: str,
) -> MetaWhatsAppOAuthOperation:
    operation = get_meta_whatsapp_oauth_operation(
        database_session, context, operation_id, for_update=True
    )
    if not secrets.compare_digest(operation.state_hash, _state_hash(state)):
        raise MetaWhatsAppOAuthStateError("invalid_oauth_state", "Invalid OAuth state")
    if operation.status != "pending":
        return operation
    operation.used_at = _now()
    operation.status = "cancelled"
    operation.reason_code = "oauth_authorization_cancelled"
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="meta_whatsapp.oauth.cancelled",
        target_membership_id=context.membership.id,
        details={"operation_id": str(operation.id)},
    )
    database_session.commit()
    return operation


def _asset_conflict(
    database_session: Session,
    operation: MetaWhatsAppOAuthOperation,
) -> bool:
    channel = database_session.scalar(
        select(WhatsAppChannel).where(
            WhatsAppChannel.provider == "meta",
            WhatsAppChannel.provider_account_id == operation.discovered_waba_id,
            WhatsAppChannel.provider_channel_id == operation.discovered_phone_number_id,
            WhatsAppChannel.tenant_id != operation.tenant_id,
        )
    )
    return channel is not None


def _different_active_channel(
    database_session: Session,
    operation: MetaWhatsAppOAuthOperation,
) -> WhatsAppChannel | None:
    return database_session.scalar(
        select(WhatsAppChannel).where(
            WhatsAppChannel.tenant_id == operation.tenant_id,
            WhatsAppChannel.provider == "meta",
            WhatsAppChannel.is_active.is_(True),
            WhatsAppChannel.provider_channel_id
            != operation.discovered_phone_number_id,
        )
    )


async def _activate_operation(
    database_session: Session,
    operation: MetaWhatsAppOAuthOperation,
    provider: MetaWhatsAppProvider,
    *,
    replace_existing: bool,
) -> MetaWhatsAppOAuthOperation:
    if _asset_conflict(database_session, operation):
        _record_failure(
            database_session,
            operation,
            reason_code="meta_whatsapp_asset_conflict",
        )
        raise MetaWhatsAppAssetConflictError
    existing_active = _different_active_channel(database_session, operation)
    if existing_active is not None and not replace_existing:
        operation.status = "confirmation_required"
        operation.reason_code = "existing_sender_confirmation_required"
        database_session.commit()
        return operation
    try:
        token = decrypt_credential(operation.credential_ciphertext or "")
    except (CredentialDecryptionError, CredentialEncryptionNotConfiguredError) as exception:
        _record_failure(
            database_session,
            operation,
            reason_code="connected_credential_unavailable",
        )
        raise MetaWhatsAppConfigurationError(
            safe_message="The connected WhatsApp credential is unavailable"
        ) from exception
    try:
        await provider.subscribe_waba(
            access_token=token,
            waba_id=operation.discovered_waba_id or "",
        )
    except MetaWhatsAppError as exception:
        operation.subscription_succeeded = False
        _record_failure(
            database_session,
            operation,
            status="subscription_failed",
            reason_code=exception.code,
            provider_http_status=exception.http_status,
        )
        return operation

    exact = database_session.scalar(
        select(WhatsAppChannel)
        .where(
            WhatsAppChannel.tenant_id == operation.tenant_id,
            WhatsAppChannel.provider == "meta",
            WhatsAppChannel.provider_account_id == operation.discovered_waba_id,
            WhatsAppChannel.provider_channel_id
            == operation.discovered_phone_number_id,
        )
        .with_for_update()
    )
    now = _now()
    if replace_existing:
        for channel in database_session.scalars(
            select(WhatsAppChannel)
            .where(
                WhatsAppChannel.tenant_id == operation.tenant_id,
                WhatsAppChannel.provider == "meta",
                WhatsAppChannel.is_active.is_(True),
                WhatsAppChannel.provider_channel_id
                != operation.discovered_phone_number_id,
            )
            .with_for_update()
        ).all():
            channel.is_active = False
    if exact is None:
        exact = WhatsAppChannel(
            tenant_id=operation.tenant_id,
            provider="meta",
            provider_account_id=operation.discovered_waba_id,
            provider_channel_id=operation.discovered_phone_number_id,
            sender_address=operation.discovered_sender_address or "",
        )
        database_session.add(exact)
    exact.sender_address = operation.discovered_sender_address or exact.sender_address
    exact.display_name = operation.discovered_display_name
    exact.credential_ciphertext = operation.credential_ciphertext
    exact.connection_mode = "coexistence"
    exact.subscription_status = "subscribed"
    exact.subscription_checked_at = now
    exact.is_active = True
    database_session.flush()
    operation.channel_id = exact.id
    operation.subscription_succeeded = True
    operation.status = "completed"
    operation.reason_code = None
    operation.confirmed_at = now if replace_existing else operation.confirmed_at
    add_authorization_audit(
        database_session,
        tenant_id=operation.tenant_id,
        actor_user_id=operation.requested_by_user_id,
        action="meta_whatsapp.oauth.completed",
        target_membership_id=None,
        details={
            "operation_id": str(operation.id),
            "channel_id": str(exact.id),
            "connection_mode": "coexistence",
            "replaced_existing": replace_existing,
        },
    )
    database_session.commit()
    database_session.refresh(operation)
    return operation


async def complete_meta_whatsapp_oauth(
    database_session: Session,
    context: WorkspaceContext,
    provider: MetaWhatsAppProvider,
    payload: MetaWhatsAppCompleteRequest,
) -> MetaWhatsAppOAuthOperation:
    operation = get_meta_whatsapp_oauth_operation(
        database_session, context, payload.operation_id, for_update=True
    )
    if operation.requested_by_user_id != context.user.id:
        raise MetaWhatsAppOAuthStateError(
            "oauth_operation_actor_mismatch",
            "WhatsApp onboarding must be completed by the initiating user",
        )
    if not secrets.compare_digest(operation.state_hash, _state_hash(payload.state)):
        raise MetaWhatsAppOAuthStateError("invalid_oauth_state", "Invalid OAuth state")
    if operation.status != "pending" or operation.used_at is not None:
        raise MetaWhatsAppOAuthStateError(
            "oauth_state_used", "OAuth state was already used"
        )
    now = _now()
    if _aware(operation.expires_at) <= now:
        operation.status = "expired"
        operation.reason_code = "oauth_state_expired"
        database_session.commit()
        raise MetaWhatsAppOAuthStateError(
            "oauth_state_expired", "OAuth state has expired"
        )
    if (
        operation.configuration_id != provider.embedded_signup_config_id
        or operation.redirect_uri != provider.oauth_redirect_uri
    ):
        operation.status = "failed"
        operation.reason_code = "oauth_configuration_changed"
        database_session.commit()
        raise MetaWhatsAppOAuthStateError(
            "oauth_configuration_changed",
            "WhatsApp onboarding configuration changed; start again",
        )
    operation.used_at = now
    operation.status = "processing"
    database_session.commit()

    try:
        token = await provider.exchange_embedded_signup_code(payload.code)
        assets = await provider.discover_coexistence_assets(
            access_token=token.access_token,
            business_id=payload.session.business_id,
            waba_id=payload.session.waba_id,
            phone_number_id=payload.session.phone_number_id,
        )
        if not assets.is_on_biz_app:
            raise MetaWhatsAppOAuthStateError(
                "coexistence_not_verified",
                "Meta did not verify this number as a WhatsApp Business app Coexistence connection",
            )
        ciphertext = encrypt_credential(token.access_token)
    except MetaWhatsAppOAuthStateError as exception:
        _record_failure(database_session, operation, reason_code=exception.code)
        raise
    except CredentialEncryptionNotConfiguredError as exception:
        _record_failure(
            database_session,
            operation,
            reason_code="credential_encryption_not_configured",
        )
        raise MetaWhatsAppConfigurationError(
            safe_message="Secure WhatsApp credential storage is not configured"
        ) from exception
    except MetaWhatsAppError as exception:
        _record_failure(
            database_session,
            operation,
            reason_code=exception.code,
            provider_http_status=exception.http_status,
        )
        raise

    operation.discovered_business_id = assets.business_id
    operation.discovered_waba_id = assets.waba_id
    operation.discovered_phone_number_id = assets.phone_number_id
    operation.discovered_sender_address = assets.sender_address
    operation.discovered_display_name = assets.display_name
    operation.connection_mode = "coexistence"
    operation.credential_ciphertext = ciphertext
    database_session.commit()
    return await _activate_operation(
        database_session,
        operation,
        provider,
        replace_existing=False,
    )


async def activate_meta_whatsapp_oauth(
    database_session: Session,
    context: WorkspaceContext,
    provider: MetaWhatsAppProvider,
    *,
    operation_id: uuid.UUID,
    confirm_replace_existing_sender: bool,
) -> MetaWhatsAppOAuthOperation:
    operation = get_meta_whatsapp_oauth_operation(
        database_session, context, operation_id, for_update=True
    )
    if operation.status not in {"confirmation_required", "subscription_failed"}:
        if operation.status == "completed":
            return operation
        raise MetaWhatsAppOAuthStateError(
            "oauth_operation_not_activatable",
            "WhatsApp onboarding operation cannot be activated",
        )
    if (
        operation.status == "confirmation_required"
        and not confirm_replace_existing_sender
    ):
        raise MetaWhatsAppOAuthStateError(
            "replacement_confirmation_required",
            "Confirm replacement of the existing active Meta WhatsApp sender",
        )
    return await _activate_operation(
        database_session,
        operation,
        provider,
        replace_existing=confirm_replace_existing_sender,
    )
