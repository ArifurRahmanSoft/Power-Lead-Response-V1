import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import WorkspaceContext
from app.models.meta_instagram_oauth import MetaInstagramOAuthState
from app.models.whatsapp import WhatsAppChannel
from app.providers.meta_instagram import (
    MetaInstagramConfigurationError,
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _state_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        raise MetaInstagramOAuthStateError("oauth_state_used", "OAuth state was already used")
    if _aware(record.expires_at) <= now:
        raise MetaInstagramOAuthStateError("oauth_state_expired", "OAuth state has expired")
    if record.redirect_uri != provider.oauth_redirect_uri:
        raise MetaInstagramOAuthStateError(
            "oauth_redirect_mismatch",
            "OAuth redirect configuration changed; start authorization again",
        )
    record.used_at = now
    database_session.commit()

    if provider_error:
        raise MetaInstagramOAuthError(
            code="oauth_authorization_denied",
            safe_message="Meta Instagram authorization was not completed",
            http_status=400,
        )
    if code is None or not code.strip() or len(code) > 4096:
        raise MetaInstagramOAuthError(
            code="oauth_code_missing",
            safe_message="Meta Instagram authorization code is missing",
            http_status=400,
        )

    token = await provider.exchange_code(code.strip())
    await provider.verify_account_access(token.access_token)
    try:
        token_ciphertext = encrypt_credential(token.access_token)
    except CredentialEncryptionNotConfiguredError as exception:
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
