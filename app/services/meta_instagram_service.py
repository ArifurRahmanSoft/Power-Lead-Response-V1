import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.email_consent import is_email_consent_denied
from app.core.rate_limit import RateLimitExceededError, meta_instagram_reply_rate_limiter
from app.core.security import WorkspaceContext
from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead
from app.models.whatsapp import (
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppDeliveryEvent,
    WhatsAppMessage,
)
from app.providers.meta_instagram import (
    MetaInstagramConfigurationError,
    MetaInstagramError,
    MetaInstagramProvider,
    MetaInstagramRejectedError,
    MetaInstagramResult,
)
from app.repositories.whatsapp_repository import (
    ConversationPage,
    MessagePage,
    get_active_instagram_channel_by_account_id,
    get_channel_for_tenant,
    get_conversation_by_contact,
    get_conversation_for_context,
    get_conversation_unread_count,
    get_message_by_idempotency,
    get_message_by_provider_sid,
    list_conversations,
    list_messages,
    list_unmatched_delivery_events,
    mark_conversation_read,
)
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.meta_instagram import (
    InstagramConversationLinkRequest,
    InstagramEligibilityData,
    InstagramReplyRequest,
)
from app.services.whatsapp_service import (
    _add_activity,
    _advance_status,
    _assigned_scope,
    conversation_contact_name,
)


INSTAGRAM_PROVIDER = "instagram"
INSTAGRAM_ID_PATTERN = re.compile(r"^[0-9]{1,32}$")
INSTAGRAM_REPLY_WINDOW = timedelta(hours=24)


class MetaInstagramWebhookRejectedError(Exception):
    pass


class MetaInstagramPayloadError(Exception):
    pass


class MetaInstagramNotFoundError(Exception):
    pass


class MetaInstagramChannelNotConfiguredError(Exception):
    pass


class MetaInstagramIdempotencyConflictError(Exception):
    pass


class MetaInstagramLinkTargetError(Exception):
    pass


class MetaInstagramEligibilityError(Exception):
    def __init__(self, code: str, message: str, **details: object) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


@dataclass(frozen=True)
class MetaInstagramSendResult:
    message: WhatsAppMessage
    replayed: bool


@dataclass(frozen=True)
class MetaInstagramRecoveryResult:
    message: WhatsAppMessage | None
    operation_state: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _max_timestamp(left: datetime, right: datetime) -> datetime:
    comparable_left = left if left.tzinfo is not None else left.replace(tzinfo=timezone.utc)
    comparable_right = right if right.tzinfo is not None else right.replace(tzinfo=timezone.utc)
    return left if comparable_left >= comparable_right else right


def page_count(total: int, size: int) -> int:
    return math.ceil(total / size) if total else 0


def account_display_name(channel: WhatsAppChannel | None) -> str:
    return channel.display_name if channel and channel.display_name else "Instagram account"


def conversation_display_name(
    database_session: Session,
    conversation: WhatsAppConversation,
    *,
    linked_name: str | None = None,
) -> tuple[str, bool, str]:
    maintained_name = linked_name
    if maintained_name is None and (conversation.lead_id or conversation.customer_id):
        maintained_name = conversation_contact_name(database_session, conversation)
    if maintained_name and maintained_name.strip():
        return maintained_name.strip(), True, "linked_record"
    if conversation.provider_display_name and conversation.provider_display_name.strip():
        return conversation.provider_display_name.strip(), True, "provider_profile"
    return "Instagram user", False, "fallback"


def channel_for_context(
    database_session: Session,
    context: WorkspaceContext,
    provider: MetaInstagramProvider,
    *,
    require_send: bool = False,
) -> WhatsAppChannel:
    if require_send and not provider.send_api_configured:
        raise MetaInstagramChannelNotConfiguredError
    channel = get_channel_for_tenant(
        database_session,
        tenant_id=context.tenant.id,
        provider=INSTAGRAM_PROVIDER,
    )
    if (
        channel is None
        or channel.provider_account_id != provider.account_id
        or channel.provider_channel_id != provider.account_id
    ):
        raise MetaInstagramChannelNotConfiguredError
    return channel


def get_instagram_conversation_record(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> WhatsAppConversation:
    conversation = get_conversation_for_context(
        database_session,
        tenant_id=context.tenant.id,
        conversation_id=conversation_id,
        assigned_scope_user_id=_assigned_scope(context),
        provider=INSTAGRAM_PROVIDER,
        for_update=for_update,
    )
    if conversation is None:
        raise MetaInstagramNotFoundError
    return conversation


def get_instagram_conversation_page(
    database_session: Session,
    context: WorkspaceContext,
    *,
    search: str | None,
    page: int,
    size: int,
) -> ConversationPage:
    return list_conversations(
        database_session,
        tenant_id=context.tenant.id,
        current_user_id=context.user.id,
        assigned_scope_user_id=_assigned_scope(context),
        search=search,
        page=page,
        size=size,
        provider=INSTAGRAM_PROVIDER,
    )


def get_instagram_message_page(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
    *,
    page: int,
    size: int,
) -> tuple[WhatsAppConversation, MessagePage]:
    conversation = get_instagram_conversation_record(
        database_session, context, conversation_id
    )
    return conversation, list_messages(
        database_session,
        conversation_id=conversation.id,
        page=page,
        size=size,
    )


def conversation_unread_count(
    database_session: Session,
    context: WorkspaceContext,
    conversation: WhatsAppConversation,
) -> int:
    return get_conversation_unread_count(
        database_session,
        conversation_id=conversation.id,
        user_id=context.user.id,
    )


def calculate_eligibility(
    database_session: Session,
    conversation: WhatsAppConversation,
) -> InstagramEligibilityData:
    inbound_at = _aware(conversation.last_inbound_at)
    expires_at = inbound_at + INSTAGRAM_REPLY_WINDOW if inbound_at else None
    restrictions: list[dict[str, str]] = []
    if expires_at is None:
        restrictions.append({
            "code": "instagram_interaction_required",
            "message": "A qualifying incoming Instagram message is required before replying",
        })
    elif expires_at <= _now():
        restrictions.append({
            "code": "instagram_reply_window_closed",
            "message": "The standard Instagram reply window is closed",
        })
    if conversation.lead_id is not None:
        lead = database_session.scalar(
            select(Lead).where(
                Lead.id == conversation.lead_id,
                Lead.tenant_id == conversation.tenant_id,
                Lead.deleted_at.is_(None),
            )
        )
        if lead is None:
            restrictions.append({"code": "lead_unavailable", "message": "The linked lead is unavailable"})
        elif is_email_consent_denied(lead.consent_status):
            restrictions.append({"code": "lead_consent_denied", "message": "The linked lead explicitly denied communication consent"})
    if conversation.customer_id is not None:
        customer = database_session.scalar(
            select(Customer).where(
                Customer.id == conversation.customer_id,
                Customer.tenant_id == conversation.tenant_id,
            )
        )
        if customer is None:
            restrictions.append({"code": "customer_unavailable", "message": "The linked customer is unavailable"})
        else:
            if is_email_consent_denied(customer.communication_consent_status):
                restrictions.append({"code": "customer_consent_denied", "message": "The linked customer explicitly denied communication consent"})
            if customer.suppressed_at is not None:
                restrictions.append({"code": "customer_suppressed", "message": "The linked customer is suppressed"})
            restricted = {str(item).strip().casefold() for item in customer.channel_restrictions}
            if restricted.intersection({"instagram", "instagram_dm", "instagram_direct"}):
                restrictions.append({"code": "instagram_channel_restricted", "message": "The linked customer restricts Instagram communication"})
    return InstagramEligibilityData(
        reply_allowed=not restrictions,
        window_expires_at=expires_at,
        blocking_reasons=restrictions,
    )


def _stored_idempotency_key(user_id: uuid.UUID, key: str) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return f"ig:{user_id}:{digest}"


def _request_hash(
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    recipient: str,
    text: str,
) -> str:
    canonical = {
        "provider": INSTAGRAM_PROVIDER,
        "user_id": str(user_id),
        "conversation_id": str(conversation_id),
        "recipient": recipient,
        "text": text,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _operation_state(message: WhatsAppMessage) -> str:
    return "in_progress" if message.submission_state == "processing" else "completed"


async def send_reply(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
    payload: InstagramReplyRequest,
    provider: MetaInstagramProvider,
) -> MetaInstagramSendResult:
    channel = channel_for_context(database_session, context, provider, require_send=True)
    conversation = get_instagram_conversation_record(database_session, context, conversation_id)
    recipient = conversation.external_address
    stored_key = _stored_idempotency_key(context.user.id, payload.idempotency_key)
    request_hash = _request_hash(
        user_id=context.user.id,
        conversation_id=conversation.id,
        recipient=recipient,
        text=payload.text,
    )
    existing = get_message_by_idempotency(
        database_session,
        tenant_id=context.tenant.id,
        idempotency_key=stored_key,
    )
    if existing is not None:
        if existing.provider != INSTAGRAM_PROVIDER or existing.request_hash != request_hash:
            raise MetaInstagramIdempotencyConflictError
        get_instagram_conversation_record(database_session, context, existing.conversation_id)
        return MetaInstagramSendResult(existing, True)

    eligibility = calculate_eligibility(database_session, conversation)
    if not eligibility.reply_allowed:
        reason = eligibility.blocking_reasons[0]
        raise MetaInstagramEligibilityError(
            reason.code,
            reason.message,
            window_expires_at=(eligibility.window_expires_at.isoformat() if eligibility.window_expires_at else None),
            blocking_reasons=[item.model_dump() for item in eligibility.blocking_reasons],
        )
    if len(payload.text.encode("utf-8")) > 1000:
        raise MetaInstagramEligibilityError(
            "message_too_long",
            "Instagram text exceeds 1000 UTF-8 bytes",
        )

    message = WhatsAppMessage(
        tenant_id=context.tenant.id,
        conversation_id=conversation.id,
        direction="outbound",
        provider=INSTAGRAM_PROVIDER,
        message_mode="text",
        body=payload.text,
        media_metadata=[],
        sender_address=channel.sender_address,
        recipient_address=recipient,
        status="unknown",
        submission_state="processing",
        idempotency_key=stored_key,
        request_hash=request_hash,
        requested_by_user_id=context.user.id,
        occurred_at=_now(),
    )
    database_session.add(message)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="meta_instagram.reply.started",
        target_membership_id=context.membership.id,
        details={"message_id": str(message.id), "conversation_id": str(conversation.id)},
    )
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        existing = get_message_by_idempotency(
            database_session,
            tenant_id=context.tenant.id,
            idempotency_key=stored_key,
        )
        if existing is not None and existing.provider == INSTAGRAM_PROVIDER and existing.request_hash == request_hash:
            return MetaInstagramSendResult(existing, True)
        if existing is not None:
            raise MetaInstagramIdempotencyConflictError from exception
        raise

    provider_result: MetaInstagramResult | None = None
    provider_error: Exception | None = None
    try:
        await meta_instagram_reply_rate_limiter.enforce(
            f"{context.tenant.id}:{context.user.id}",
            limit=settings.meta_instagram_reply_rate_limit_requests,
            window_seconds=settings.meta_instagram_reply_rate_limit_window_seconds,
        )
        stored = database_session.get(WhatsAppMessage, message.id)
        if stored is None:
            raise MetaInstagramNotFoundError
        stored.provider_request_started_at = _now()
        database_session.commit()
        provider_result = await provider.send_text(to=recipient, body=payload.text)
    except RateLimitExceededError:
        provider_error = MetaInstagramRejectedError(
            code="application_rate_limit",
            safe_message="Instagram reply rate limit exceeded",
            http_status=429,
        )
    except Exception as exception:
        provider_error = exception

    for attempt in range(2):
        try:
            stored = database_session.get(WhatsAppMessage, message.id)
            stored_conversation = database_session.get(WhatsAppConversation, conversation.id)
            if stored is None or stored_conversation is None:
                raise MetaInstagramNotFoundError
            completed = _now()
            if provider_result is not None:
                existing_mid = get_message_by_provider_sid(
                    database_session,
                    provider_result.message_id,
                    provider=INSTAGRAM_PROVIDER,
                )
                if existing_mid is not None and existing_mid.id != stored.id:
                    raise MetaInstagramEligibilityError(
                        "provider_message_conflict",
                        "The provider message identifier is already reconciled",
                    )
                stored.provider_message_sid = provider_result.message_id
                stored.status = _advance_status(stored.status, provider_result.status)
            elif isinstance(provider_error, (MetaInstagramRejectedError, MetaInstagramConfigurationError)):
                stored.status = "failed"
                stored.provider_error_code = provider_error.code
                stored.failure_reason = provider_error.safe_message
            else:
                stored.status = "unknown"
                stored.provider_error_code = "acceptance_unknown"
                stored.failure_reason = "Instagram message acceptance could not be determined"
            stored.submission_state = "completed"
            stored.completed_at = completed
            stored_conversation.last_message_at = _max_timestamp(stored_conversation.last_message_at, completed)
            stored_conversation.last_message_preview = stored.body[:240]
            if stored.provider_message_sid:
                for event in list_unmatched_delivery_events(
                    database_session,
                    provider_message_sid=stored.provider_message_sid,
                    provider=INSTAGRAM_PROVIDER,
                ):
                    event.message_id = stored.id
                    stored.status = _advance_status(stored.status, event.status)
            _add_activity(database_session, conversation=stored_conversation, message=stored)
            database_session.commit()
            database_session.refresh(stored)
            return MetaInstagramSendResult(stored, False)
        except SQLAlchemyError:
            database_session.rollback()
            if attempt == 1:
                raise
    raise RuntimeError("unreachable")


def recover_operation(
    database_session: Session,
    context: WorkspaceContext,
    idempotency_key: str,
) -> MetaInstagramRecoveryResult:
    message = get_message_by_idempotency(
        database_session,
        tenant_id=context.tenant.id,
        idempotency_key=_stored_idempotency_key(context.user.id, idempotency_key),
        provider=INSTAGRAM_PROVIDER,
    )
    if message is None:
        return MetaInstagramRecoveryResult(None, "missing")
    get_instagram_conversation_record(database_session, context, message.conversation_id)
    return MetaInstagramRecoveryResult(message, _operation_state(message))


def mark_read(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
):
    conversation = get_instagram_conversation_record(database_session, context, conversation_id)
    cursor = mark_conversation_read(
        database_session,
        conversation_id=conversation.id,
        user_id=context.user.id,
    )
    database_session.commit()
    database_session.refresh(cursor)
    return cursor


def link_conversation(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
    payload: InstagramConversationLinkRequest,
) -> WhatsAppConversation:
    conversation = get_instagram_conversation_record(
        database_session, context, conversation_id, for_update=True
    )
    if payload.lead_id is not None:
        conditions = [
            Lead.id == payload.lead_id,
            Lead.tenant_id == context.tenant.id,
            Lead.deleted_at.is_(None),
        ]
        if _assigned_scope(context) is not None:
            conditions.append(Lead.assigned_user_id == context.user.id)
        lead = database_session.scalar(select(Lead).where(*conditions))
        if lead is None:
            raise MetaInstagramLinkTargetError
        conversation.lead_id, conversation.customer_id = lead.id, None
    else:
        customer = database_session.scalar(
            select(Customer).where(
                Customer.id == payload.customer_id,
                Customer.tenant_id == context.tenant.id,
            )
        )
        if customer is None:
            raise MetaInstagramLinkTargetError
        if _assigned_scope(context) is not None:
            visible = database_session.scalar(
                select(LeadConversion.id)
                .join(Lead, Lead.id == LeadConversion.lead_id)
                .where(
                    LeadConversion.customer_id == customer.id,
                    LeadConversion.tenant_id == context.tenant.id,
                    Lead.assigned_user_id == context.user.id,
                )
                .limit(1)
            )
            if visible is None:
                raise MetaInstagramLinkTargetError
        conversation.customer_id, conversation.lead_id = customer.id, None
    database_session.flush()
    for message in database_session.scalars(
        select(WhatsAppMessage).where(
            WhatsAppMessage.conversation_id == conversation.id,
            WhatsAppMessage.provider == INSTAGRAM_PROVIDER,
        )
    ).all():
        _add_activity(database_session, conversation=conversation, message=message)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="meta_instagram.conversation.linked",
        target_membership_id=context.membership.id,
        details={
            "conversation_id": str(conversation.id),
            "lead_id": str(conversation.lead_id) if conversation.lead_id else None,
            "customer_id": str(conversation.customer_id) if conversation.customer_id else None,
        },
    )
    database_session.commit()
    database_session.refresh(conversation)
    return conversation


def _timestamp_ms(value: object) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return _now()


def _provider_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not INSTAGRAM_ID_PATTERN.fullmatch(normalized):
        raise MetaInstagramPayloadError
    return normalized


def _provider_display_name(value: object) -> str | None:
    """Use provider-supplied profile text only when it is present in the event.

    Unified Login webhooks normally identify contacts by IGSID. Some event
    variants may include a permitted username/name beside that identifier; the
    value is treated only as a display hint and never as an identity key.
    """
    if not isinstance(value, dict):
        return None
    for key in ("name", "username"):
        candidate = value.get(key)
        if not isinstance(candidate, str):
            continue
        normalized = candidate.strip()
        if normalized and len(normalized) <= 200 and not any(
            ord(character) < 32 for character in normalized
        ):
            return normalized
    return None


def _attachment_metadata(message: dict[str, Any]) -> list[dict[str, object]]:
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return []
    output: list[dict[str, object]] = []
    allowed = {"audio", "file", "image", "video", "media", "ig_post", "story"}
    for attachment in attachments[:10]:
        if not isinstance(attachment, dict):
            continue
        raw_type = str(attachment.get("type") or "attachment").strip().casefold()
        safe_type = raw_type if raw_type in allowed else "attachment"
        output.append({"type": safe_type, "unsupported": True})
    return output


def _message_body(message: dict[str, Any], media: list[dict[str, object]]) -> str:
    raw_text = message.get("text")
    text = str(raw_text)[:10_000] if isinstance(raw_text, str) else ""
    if message.get("is_deleted") is True:
        return "[Instagram message deleted]"
    placeholders = [f"[Instagram {item['type']} attachment]" for item in media]
    if message.get("is_unsupported") is True and not placeholders:
        placeholders.append("[Unsupported Instagram message]")
    parts = [part for part in [text, *placeholders] if part]
    return "\n".join(parts) or "[Unsupported Instagram message]"


def _event_key(
    *,
    channel_id: uuid.UUID,
    provider_message_id: str,
    status: str,
) -> str:
    return hashlib.sha256(
        "|".join((INSTAGRAM_PROVIDER, str(channel_id), provider_message_id, status)).encode()
    ).hexdigest()


def _persist_event(
    database_session: Session,
    *,
    channel: WhatsAppChannel,
    provider_message_id: str,
    status: str,
    occurred_at: datetime,
) -> bool:
    key = _event_key(
        channel_id=channel.id,
        provider_message_id=provider_message_id,
        status=status,
    )
    if database_session.scalar(
        select(WhatsAppDeliveryEvent.id).where(WhatsAppDeliveryEvent.event_key == key)
    ) is not None:
        return False
    message = get_message_by_provider_sid(
        database_session,
        provider_message_id,
        provider=INSTAGRAM_PROVIDER,
    )
    if message is not None and message.tenant_id != channel.tenant_id:
        raise MetaInstagramWebhookRejectedError
    database_session.add(
        WhatsAppDeliveryEvent(
            tenant_id=channel.tenant_id,
            channel_id=channel.id,
            message_id=message.id if message else None,
            provider=INSTAGRAM_PROVIDER,
            provider_message_sid=provider_message_id,
            event_key=key,
            status=status,
            occurred_at=occurred_at,
        )
    )
    if message is not None:
        message.status = _advance_status(message.status, status)
    return True


def _persist_inbound_message(
    database_session: Session,
    *,
    channel: WhatsAppChannel,
    account_id: str,
    sender_id: str,
    event: dict[str, Any],
    message_payload: dict[str, Any],
) -> str:
    provider_message_id = str(message_payload.get("mid") or "").strip()
    if not provider_message_id or len(provider_message_id) > 255:
        raise MetaInstagramPayloadError
    existing = get_message_by_provider_sid(
        database_session,
        provider_message_id,
        provider=INSTAGRAM_PROVIDER,
    )
    if existing is not None:
        if existing.tenant_id != channel.tenant_id:
            raise MetaInstagramWebhookRejectedError
        return "duplicate"
    occurred_at = _timestamp_ms(event.get("timestamp"))
    conversation = get_conversation_by_contact(
        database_session,
        channel_id=channel.id,
        external_address=sender_id,
        for_update=True,
    )
    if conversation is None:
        conversation = WhatsAppConversation(
            tenant_id=channel.tenant_id,
            channel_id=channel.id,
            external_address=sender_id,
            last_message_at=occurred_at,
            last_inbound_at=occurred_at,
        )
        database_session.add(conversation)
        database_session.flush()
    supplied_name = _provider_display_name(event.get("sender"))
    if supplied_name and not conversation.provider_display_name:
        conversation.provider_display_name = supplied_name
        conversation.provider_profile_refreshed_at = _now()
    media = _attachment_metadata(message_payload)
    body = _message_body(message_payload, media)
    message = WhatsAppMessage(
        tenant_id=channel.tenant_id,
        conversation_id=conversation.id,
        direction="inbound",
        provider=INSTAGRAM_PROVIDER,
        message_mode="text",
        body=body,
        media_metadata=media,
        sender_address=sender_id,
        recipient_address=account_id,
        provider_message_sid=provider_message_id,
        status="received",
        submission_state="received",
        occurred_at=occurred_at,
        completed_at=occurred_at,
    )
    database_session.add(message)
    database_session.flush()
    conversation.last_message_at = _max_timestamp(conversation.last_message_at, occurred_at)
    if conversation.last_inbound_at is None or occurred_at > conversation.last_inbound_at:
        conversation.last_inbound_at = occurred_at
    conversation.last_message_preview = body[:240]
    _add_activity(database_session, conversation=conversation, message=message)
    return "created"


def _persist_outbound_echo(
    database_session: Session,
    *,
    channel: WhatsAppChannel,
    account_id: str,
    recipient_id: str,
    event: dict[str, Any],
    message_payload: dict[str, Any],
) -> str:
    provider_message_id = str(message_payload.get("mid") or "").strip()
    if not provider_message_id or len(provider_message_id) > 255:
        raise MetaInstagramPayloadError
    occurred_at = _timestamp_ms(event.get("timestamp"))
    existing = get_message_by_provider_sid(
        database_session,
        provider_message_id,
        provider=INSTAGRAM_PROVIDER,
    )
    if existing is not None:
        if existing.tenant_id != channel.tenant_id:
            raise MetaInstagramWebhookRejectedError
        _persist_event(
            database_session,
            channel=channel,
            provider_message_id=provider_message_id,
            status="accepted",
            occurred_at=occurred_at,
        )
        return "reconciled"
    body = _message_body(message_payload, _attachment_metadata(message_payload))
    local_message = database_session.scalar(
        select(WhatsAppMessage)
        .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
        .where(
            WhatsAppMessage.tenant_id == channel.tenant_id,
            WhatsAppMessage.provider == INSTAGRAM_PROVIDER,
            WhatsAppMessage.direction == "outbound",
            WhatsAppMessage.submission_state == "processing",
            WhatsAppMessage.provider_message_sid.is_(None),
            WhatsAppMessage.recipient_address == recipient_id,
            WhatsAppMessage.body == body,
            WhatsAppConversation.channel_id == channel.id,
        )
        .order_by(WhatsAppMessage.occurred_at.desc())
        .with_for_update()
        .limit(1)
    )
    if local_message is not None:
        local_message.provider_message_sid = provider_message_id
        local_message.status = _advance_status(local_message.status, "accepted")
        _persist_event(
            database_session,
            channel=channel,
            provider_message_id=provider_message_id,
            status="accepted",
            occurred_at=occurred_at,
        )
        return "reconciled"
    conversation = get_conversation_by_contact(
        database_session,
        channel_id=channel.id,
        external_address=recipient_id,
        for_update=True,
    )
    if conversation is None:
        conversation = WhatsAppConversation(
            tenant_id=channel.tenant_id,
            channel_id=channel.id,
            external_address=recipient_id,
            last_message_at=occurred_at,
        )
        database_session.add(conversation)
        database_session.flush()
    media = _attachment_metadata(message_payload)
    external = WhatsAppMessage(
        tenant_id=channel.tenant_id,
        conversation_id=conversation.id,
        direction="outbound",
        provider=INSTAGRAM_PROVIDER,
        message_mode="text",
        body=body,
        media_metadata=media,
        sender_address=account_id,
        recipient_address=recipient_id,
        provider_message_sid=provider_message_id,
        status="accepted",
        submission_state="completed",
        occurred_at=occurred_at,
        completed_at=occurred_at,
    )
    database_session.add(external)
    database_session.flush()
    conversation.last_message_at = _max_timestamp(conversation.last_message_at, occurred_at)
    conversation.last_message_preview = body[:240]
    _persist_event(
        database_session,
        channel=channel,
        provider_message_id=provider_message_id,
        status="accepted",
        occurred_at=occurred_at,
    )
    _add_activity(database_session, conversation=conversation, message=external)
    return "external"


def _process_messaging_event(
    database_session: Session,
    *,
    channel: WhatsAppChannel,
    account_id: str,
    event: dict[str, Any],
    counts: dict[str, int],
) -> None:
    message_payload = event.get("message")
    read = event.get("read")
    if not isinstance(message_payload, dict) and not isinstance(read, dict):
        counts["unsupported"] += 1
        return
    sender = event.get("sender")
    recipient = event.get("recipient")
    sender_id = _provider_id(sender.get("id") if isinstance(sender, dict) else None)
    recipient_id = _provider_id(recipient.get("id") if isinstance(recipient, dict) else None)
    occurred_at = _timestamp_ms(event.get("timestamp"))
    if isinstance(message_payload, dict):
        if message_payload.get("is_echo") is True:
            if sender_id != account_id:
                raise MetaInstagramWebhookRejectedError
            outcome = _persist_outbound_echo(
                database_session,
                channel=channel,
                account_id=account_id,
                recipient_id=recipient_id,
                event=event,
                message_payload=message_payload,
            )
            counts["events"] += 1
            counts["echoes"] += 1
            if outcome == "external":
                counts["messages"] += 1
            return
        if recipient_id != account_id:
            raise MetaInstagramWebhookRejectedError
        outcome = _persist_inbound_message(
            database_session,
            channel=channel,
            account_id=account_id,
            sender_id=sender_id,
            event=event,
            message_payload=message_payload,
        )
        counts["messages" if outcome == "created" else "duplicates"] += 1
        return
    if recipient_id != account_id:
        raise MetaInstagramWebhookRejectedError
    provider_message_id = str(read.get("mid") or "").strip()
    if not provider_message_id or len(provider_message_id) > 255:
        counts["unsupported"] += 1
        return
    if _persist_event(
        database_session,
        channel=channel,
        provider_message_id=provider_message_id,
        status="read",
        occurred_at=occurred_at,
    ):
        counts["events"] += 1
    else:
        counts["duplicates"] += 1


def process_instagram_webhook_payload(
    database_session: Session,
    provider: MetaInstagramProvider,
    payload: dict[str, Any],
    *,
    _retry_on_conflict: bool = True,
) -> dict[str, int]:
    entries = payload.get("entry")
    if (
        not provider.webhook_configured
        or payload.get("object") != "instagram"
        or not isinstance(entries, list)
    ):
        raise MetaInstagramWebhookRejectedError
    counts = {"messages": 0, "events": 0, "echoes": 0, "duplicates": 0, "unsupported": 0}
    for entry in entries:
        if not isinstance(entry, dict):
            raise MetaInstagramPayloadError
        account_id = str(entry.get("id") or "").strip()
        if account_id != provider.account_id:
            raise MetaInstagramWebhookRejectedError
        channel = get_active_instagram_channel_by_account_id(
            database_session,
            account_id=account_id,
        )
        if channel is None:
            raise MetaInstagramWebhookRejectedError
        messaging = entry.get("messaging")
        if isinstance(messaging, list):
            for event in messaging:
                if isinstance(event, dict):
                    _process_messaging_event(
                        database_session,
                        channel=channel,
                        account_id=account_id,
                        event=event,
                        counts=counts,
                    )
                else:
                    counts["unsupported"] += 1
        changes = entry.get("changes")
        if isinstance(changes, list):
            counts["unsupported"] += len(changes)
    try:
        database_session.commit()
    except IntegrityError:
        database_session.rollback()
        if _retry_on_conflict:
            return process_instagram_webhook_payload(
                database_session,
                provider,
                payload,
                _retry_on_conflict=False,
            )
        raise
    return counts
