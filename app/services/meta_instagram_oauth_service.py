import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import WorkspaceContext
from app.models.meta_instagram_oauth import MetaInstagramOAuthState
from app.models.whatsapp import WhatsAppChannel
from app.providers.meta_instagram import (
    MetaInstagramConfigurationError,
    MetaInstagramError,
    MetaInstagramOAuthError,
    MetaInstagramProvider,
)
from app.repositories.whatsapp_repository import get_channel_for_tenant
from app.repositories.workspace_repository import add_authorization_audit
from app.utils.credential_encryption import (
    CredentialEncryptionNotConfiguredError,
    encrypt_credential,
)


class MetaInstagramOAuthStateError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class MetaInstagramOAuthStart:
    authorization_url: str
    expires_at: datetime


@dataclass(frozen=True)
class MetaInstagramOAuthCompletion:
    tenant_id: uuid.UUID
    channel_id: uuid.UUID
    account_id: str


@dataclass(frozen=True)
class MetaInstagramOAuthRedirectDiagnostic:
    configured_scheme: str | None
    configured_host: str | None
    configured_path: str | None
    configured_has_query: bool
    configured_has_fragment: bool
    latest_operation_found: bool
    operation_scheme: str | None
    operation_host: str | None
    operation_path: str | None
    operation_has_query: bool | None
    operation_has_fragment: bool | None
    exact_match: bool | None
    scheme_match: bool | None
    host_match: bool | None
    path_match: bool | None
    trailing_slash_match: bool | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _state_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def oauth_redirect_diagnostic(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    configured_redirect_uri: str,
) -> MetaInstagramOAuthRedirectDiagnostic:
    """Return safe redirect metadata without exposing state, code, or tokens."""
    latest = database_session.scalar(
        select(MetaInstagramOAuthState)
        .where(MetaInstagramOAuthState.tenant_id == tenant_id)
        .order_by(
            MetaInstagramOAuthState.created_at.desc(),
            MetaInstagramOAuthState.id.desc(),
        )
        .limit(1)
    )
    configured = urlsplit(configured_redirect_uri)
    operation = urlsplit(latest.redirect_uri) if latest is not None else None
    return MetaInstagramOAuthRedirectDiagnostic(
        configured_scheme=configured.scheme or None,
        configured_host=configured.hostname,
        configured_path=configured.path or None,
        configured_has_query=bool(configured.query),
        configured_has_fragment=bool(configured.fragment),
        latest_operation_found=latest is not None,
        operation_scheme=(operation.scheme or None) if operation else None,
        operation_host=operation.hostname if operation else None,
        operation_path=(operation.path or None) if operation else None,
        operation_has_query=bool(operation.query) if operation else None,
        operation_has_fragment=bool(operation.fragment) if operation else None,
        exact_match=(latest.redirect_uri == configured_redirect_uri) if latest else None,
        scheme_match=(operation.scheme == configured.scheme) if operation else None,
        host_match=(operation.hostname == configured.hostname) if operation else None,
        path_match=(operation.path == configured.path) if operation else None,
        trailing_slash_match=(
            operation.path.endswith("/") == configured.path.endswith("/")
            if operation
            else None
        ),
    )


def _record_failure(
    database_session: Session,
    record: MetaInstagramOAuthState,
    *,
    stage: str,
    code: str,
    provider_http_status: int | None = None,
    provider_code: str | None = None,
    provider_subcode: str | None = None,
    error_category: str | None = None,
) -> None:
    details: dict[str, object] = {
        "channel_id": str(record.channel_id),
        "stage": stage,
        "reason_code": code,
    }
    if provider_http_status is not None:
        details["provider_http_status"] = provider_http_status
    if provider_code is not None:
        details["provider_code"] = provider_code
    if provider_subcode is not None:
        details["provider_subcode"] = provider_subcode
    if error_category is not None:
        details["error_category"] = error_category
    add_authorization_audit(
        database_session,
        tenant_id=record.tenant_id,
        actor_user_id=record.requested_by_user_id,
        action="meta_instagram.oauth.failed",
        target_membership_id=None,
        details=details,
    )
    database_session.commit()


def _record_rejection(
    database_session: Session,
    record: MetaInstagramOAuthState,
    *,
    code: str,
) -> None:
    add_authorization_audit(
        database_session,
        tenant_id=record.tenant_id,
        actor_user_id=record.requested_by_user_id,
        action="meta_instagram.oauth.rejected",
        target_membership_id=None,
        details={
            "channel_id": str(record.channel_id),
            "stage": "state_validation",
            "reason_code": code,
        },
    )
    database_session.commit()


def begin_instagram_oauth(
    database_session: Session,
    context: WorkspaceContext,
    provider: MetaInstagramProvider,
    *,
    expire_minutes: int,
) -> MetaInstagramOAuthStart:
    if not provider.oauth_configured:
        raise MetaInstagramConfigurationError(
            safe_message="Meta Instagram Unified Login OAuth is not configured"
        )
    channel = get_channel_for_tenant(
        database_session,
        tenant_id=context.tenant.id,
        provider="instagram",
    )
    if (
        channel is None
        or channel.provider_account_id != provider.account_id
        or channel.provider_channel_id != provider.account_id
    ):
        raise MetaInstagramConfigurationError(
            safe_message="The workspace Instagram account channel is not configured"
        )
    raw_state = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(minutes=expire_minutes)
    database_session.add(
        MetaInstagramOAuthState(
            tenant_id=context.tenant.id,
            requested_by_user_id=context.user.id,
            channel_id=channel.id,
            state_hash=_state_hash(raw_state),
            redirect_uri=provider.oauth_redirect_uri,
            expires_at=expires_at,
        )
    )
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="meta_instagram.oauth.started",
        target_membership_id=context.membership.id,
        details={"channel_id": str(channel.id)},
    )
    database_session.commit()
    return MetaInstagramOAuthStart(
        authorization_url=provider.authorization_url(state=raw_state),
        expires_at=expires_at,
    )


async def complete_instagram_oauth(
    database_session: Session,
    provider: MetaInstagramProvider,
    *,
    state: str,
    code: str | None,
    provider_error: str | None,
) -> MetaInstagramOAuthCompletion:
    if not state or len(state) > 512:
        raise MetaInstagramOAuthStateError("invalid_oauth_state", "Invalid OAuth state")
    record = database_session.scalar(
        select(MetaInstagramOAuthState)
        .where(MetaInstagramOAuthState.state_hash == _state_hash(state))
        .with_for_update()
    )
    if record is None:
        raise MetaInstagramOAuthStateError("invalid_oauth_state", "Invalid OAuth state")
    now = _now()
    if record.used_at is not None:
        _record_rejection(database_session, record, code="oauth_state_used")
        raise MetaInstagramOAuthStateError("oauth_state_used", "OAuth state was already used")
    if _aware(record.expires_at) <= now:
        _record_rejection(database_session, record, code="oauth_state_expired")
        raise MetaInstagramOAuthStateError("oauth_state_expired", "OAuth state has expired")
    if record.redirect_uri != provider.oauth_redirect_uri:
        _record_rejection(database_session, record, code="oauth_redirect_mismatch")
        raise MetaInstagramOAuthStateError(
            "oauth_redirect_mismatch",
            "OAuth redirect configuration changed; start authorization again",
        )
    record.used_at = now
    database_session.commit()

    if provider_error:
        _record_failure(
            database_session,
            record,
            stage="authorization",
            code="oauth_authorization_denied",
        )
        raise MetaInstagramOAuthError(
            code="oauth_authorization_denied",
            safe_message="Meta Instagram authorization was not completed",
            http_status=400,
        )
    if code is None or not code.strip() or len(code) > 4096:
        _record_failure(
            database_session,
            record,
            stage="callback_validation",
            code="oauth_code_missing",
        )
        raise MetaInstagramOAuthError(
            code="oauth_code_missing",
            safe_message="Meta Instagram authorization code is missing",
            http_status=400,
        )

    try:
        token = await provider.exchange_code(
            code.strip(),
            redirect_uri=record.redirect_uri,
        )
    except MetaInstagramError as exception:
        _record_failure(
            database_session,
            record,
            stage="code_exchange",
            code=exception.code,
            provider_http_status=exception.http_status,
            provider_code=exception.provider_code,
            provider_subcode=exception.provider_subcode,
            error_category=exception.error_category,
        )
        raise
    try:
        await provider.verify_account_access(token.access_token)
    except MetaInstagramError as exception:
        _record_failure(
            database_session,
            record,
            stage="account_verification",
            code=exception.code,
            provider_http_status=exception.http_status,
            provider_code=exception.provider_code,
            provider_subcode=exception.provider_subcode,
            error_category=exception.error_category,
        )
        raise
    try:
        token_ciphertext = encrypt_credential(token.access_token)
    except CredentialEncryptionNotConfiguredError as exception:
        _record_failure(
            database_session,
            record,
            stage="credential_encryption",
            code="credential_encryption_not_configured",
        )
        raise MetaInstagramConfigurationError(
            safe_message="Secure Instagram credential storage is not configured"
        ) from exception

    channel = database_session.scalar(
        select(WhatsAppChannel)
        .where(
            WhatsAppChannel.id == record.channel_id,
            WhatsAppChannel.tenant_id == record.tenant_id,
            WhatsAppChannel.provider == "instagram",
            WhatsAppChannel.provider_account_id == provider.account_id,
            WhatsAppChannel.provider_channel_id == provider.account_id,
            WhatsAppChannel.is_active.is_(True),
        )
        .with_for_update()
    )
    if channel is None:
        _record_failure(
            database_session,
            record,
            stage="credential_persistence",
            code="oauth_channel_unavailable",
        )
        raise MetaInstagramOAuthStateError(
            "oauth_channel_unavailable",
            "The Instagram channel is no longer available",
        )
    channel.credential_ciphertext = token_ciphertext
    add_authorization_audit(
        database_session,
        tenant_id=record.tenant_id,
        actor_user_id=record.requested_by_user_id,
        action="meta_instagram.oauth.completed",
        target_membership_id=None,
        details={"channel_id": str(channel.id), "account_id": provider.account_id},
    )
    database_session.commit()
    return MetaInstagramOAuthCompletion(
        tenant_id=record.tenant_id,
        channel_id=channel.id,
        account_id=provider.account_id,
    )
