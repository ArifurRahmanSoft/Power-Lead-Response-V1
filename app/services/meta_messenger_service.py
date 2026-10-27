import hashlib
import asyncio
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
from app.core.rate_limit import (
    RateLimitExceededError,
    meta_messenger_profile_refresh_rate_limiter,
    meta_messenger_reply_rate_limiter,
)
from app.core.security import WorkspaceContext
from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead
from app.models.whatsapp import (
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppDeliveryEvent,
    WhatsAppMessage,
)
from app.providers.meta_messenger import (
    MetaMessengerConfigurationError,
    MetaMessengerError,
    MetaMessengerProvider,
    MetaMessengerRejectedError,
    MetaMessengerResult,
)
from app.repositories.whatsapp_repository import (
    ConversationPage,
    MessagePage,
    get_active_messenger_channel_by_page_id,
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
from app.schemas.meta_messenger import (
    MessengerConversationLinkRequest,
    MessengerEligibilityData,
    MessengerProfileRefreshData,
    MessengerProfileRefreshItem,
    MessengerProfileRefreshRequest,
    MessengerReplyRequest,
)
from app.services.whatsapp_service import (
    _add_activity,
    _advance_status,
    _assigned_scope,
    conversation_contact_name,
)


MESSENGER_PROVIDER = "messenger"
MESSENGER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MESSENGER_REPLY_WINDOW = timedelta(hours=24)


class MetaMessengerWebhookRejectedError(Exception):
    pass


class MetaMessengerPayloadError(Exception):
    pass


class MetaMessengerNotFoundError(Exception):
    pass


class MetaMessengerChannelNotConfiguredError(Exception):
    pass


class MetaMessengerIdempotencyConflictError(Exception):
    pass


class MetaMessengerLinkTargetError(Exception):
    pass


class MetaMessengerEligibilityError(Exception):
    def __init__(self, code: str, message: str, **details: object) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


@dataclass(frozen=True)
class MetaMessengerSendResult:
    message: WhatsAppMessage
    replayed: bool


@dataclass(frozen=True)
class MetaMessengerRecoveryResult:
    message: WhatsAppMessage | None
    operation_state: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _max_timestamp(left: datetime, right: datetime) -> datetime:
    comparable_left = left if left.tzinfo is not None else left.replace(tzinfo=timezone.utc)
    comparable_right = right if right.tzinfo is not None else right.replace(tzinfo=timezone.utc)
    return left if comparable_left >= comparable_right else right


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def page_count(total: int, size: int) -> int:
    return math.ceil(total / size) if total else 0


def channel_for_context(
    database_session: Session,
    context: WorkspaceContext,
    provider: MetaMessengerProvider,
    *,
    require_send: bool = False,
) -> WhatsAppChannel:
    if require_send and not provider.send_api_configured:
        raise MetaMessengerChannelNotConfiguredError
    channel = get_channel_for_tenant(
        database_session,
        tenant_id=context.tenant.id,
        provider=MESSENGER_PROVIDER,
    )
    if (
        channel is None
        or channel.provider_account_id != provider.page_id
        or channel.provider_channel_id != provider.page_id
    ):
        raise MetaMessengerChannelNotConfiguredError
    return channel


def get_messenger_conversation_record(
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
        provider=MESSENGER_PROVIDER,
        for_update=for_update,
    )
    if conversation is None:
        raise MetaMessengerNotFoundError
    return conversation


def get_messenger_conversation_page(
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
        provider=MESSENGER_PROVIDER,
    )


def get_messenger_message_page(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
    *,
    page: int,
    size: int,
) -> tuple[WhatsAppConversation, MessagePage]:
    conversation = get_messenger_conversation_record(
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


def page_display_name(channel: WhatsAppChannel | None) -> str:
    return channel.display_name if channel and channel.display_name else "Facebook Page"


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
    return "Messenger user", False, "fallback"


def _cache_is_fresh(refreshed_at: datetime | None) -> bool:
    value = _aware(refreshed_at)
    return bool(
        value
        and value + timedelta(hours=settings.meta_messenger_profile_cache_hours) > _now()
    )


def _retry_is_blocked(retry_after: datetime | None) -> bool:
    value = _aware(retry_after)
    return bool(value and value > _now())


async def refresh_display_names(
    database_session: Session,
    context: WorkspaceContext,
    payload: MessengerProfileRefreshRequest,
    provider: MetaMessengerProvider,
) -> MessengerProfileRefreshData:
    channel = channel_for_context(database_session, context, provider, require_send=True)
    try:
        await meta_messenger_profile_refresh_rate_limiter.enforce(
            f"{context.tenant.id}:{context.user.id}",
            limit=settings.meta_messenger_profile_refresh_rate_limit_requests,
            window_seconds=settings.meta_messenger_profile_refresh_rate_limit_window_seconds,
        )
    except RateLimitExceededError as exception:
        raise MetaMessengerEligibilityError(
            "profile_refresh_rate_limited",
            "Messenger profile refresh rate limit exceeded",
        ) from exception

    if len(payload.conversation_ids) > settings.meta_messenger_profile_refresh_limit:
        raise MetaMessengerEligibilityError(
            "profile_refresh_limit_exceeded",
            f"At most {settings.meta_messenger_profile_refresh_limit} conversations may be refreshed",
        )
    conversation_ids: list[uuid.UUID] = []
    cached_items: list[MessengerProfileRefreshItem] = []
    if payload.conversation_ids:
        for conversation_id in dict.fromkeys(payload.conversation_ids):
            conversation = get_messenger_conversation_record(
                database_session, context, conversation_id
            )
            display_name, available, _ = conversation_display_name(
                database_session, conversation
            )
            if _cache_is_fresh(conversation.provider_profile_refreshed_at):
                cached_items.append(MessengerProfileRefreshItem(
                    conversation_id=conversation.id,
                    status="cached",
                    contact_display_name=display_name,
                    display_name_available=available,
                ))
            elif _retry_is_blocked(conversation.provider_profile_retry_after):
                cached_items.append(MessengerProfileRefreshItem(
                    conversation_id=conversation.id,
                    status="cached" if conversation.provider_display_name else "unavailable",
                    contact_display_name=display_name,
                    display_name_available=available,
                    reason_code=conversation.provider_profile_error_code,
                ))
            else:
                conversation_ids.append(conversation.id)
    else:
        scan = get_messenger_conversation_page(
            database_session,
            context,
            search=None,
            page=1,
            size=min(100, settings.meta_messenger_profile_refresh_limit * 4),
        )
        for item in scan.items:
            conversation = item.conversation
            if item.lead_name or item.customer_name:
                continue
            if _cache_is_fresh(conversation.provider_profile_refreshed_at):
                continue
            if _retry_is_blocked(conversation.provider_profile_retry_after):
                continue
            conversation_ids.append(conversation.id)
            if len(conversation_ids) >= settings.meta_messenger_profile_refresh_limit:
                break

    refresh_page = payload.refresh_page and not _cache_is_fresh(channel.profile_refreshed_at)
    page_blocked = _retry_is_blocked(channel.profile_retry_after)
    page_status = "skipped"
    if not payload.refresh_page:
        page_status = "skipped"
    elif not refresh_page:
        page_status = "cached"
    elif page_blocked:
        page_status = "cached" if channel.display_name else "unavailable"

    requested_profiles = [
        (conversation_id, get_messenger_conversation_record(
            database_session, context, conversation_id
        ).external_address)
        for conversation_id in conversation_ids
    ]
    database_session.commit()

    page_result: tuple[str, object] | None = None
    if refresh_page and not page_blocked:
        try:
            page_result = ("success", await provider.get_page_profile())
        except MetaMessengerError as exception:
            page_result = ("error", exception)

    semaphore = asyncio.Semaphore(settings.meta_messenger_profile_refresh_concurrency)

    async def fetch_profile(conversation_id: uuid.UUID, psid: str):
        async with semaphore:
            try:
                return conversation_id, "success", await provider.get_user_profile(psid=psid)
            except MetaMessengerError as exception:
                return conversation_id, "error", exception

    profile_results = await asyncio.gather(
        *(fetch_profile(conversation_id, psid) for conversation_id, psid in requested_profiles)
    )
    now = _now()
    retry_after = now + timedelta(seconds=settings.meta_messenger_profile_retry_seconds)
    stored_channel = database_session.get(WhatsAppChannel, channel.id)
    if stored_channel is None or stored_channel.tenant_id != context.tenant.id:
        raise MetaMessengerChannelNotConfiguredError
    if page_result is not None:
        if page_result[0] == "success":
            stored_channel.display_name = page_result[1].display_name
            stored_channel.profile_refreshed_at = now
            stored_channel.profile_retry_after = None
            page_status = "refreshed"
        else:
            stored_channel.profile_retry_after = retry_after
            page_status = "cached" if stored_channel.display_name else "unavailable"

    items: list[MessengerProfileRefreshItem] = list(cached_items)
    refreshed_count = 0
    unavailable_count = 0
    for conversation_id, outcome, result in profile_results:
        conversation = get_messenger_conversation_record(
            database_session, context, conversation_id, for_update=True
        )
        if outcome == "success":
            conversation.provider_display_name = result.display_name
            conversation.provider_profile_refreshed_at = now
            conversation.provider_profile_retry_after = None
            conversation.provider_profile_error_code = None
            status = "refreshed"
            reason_code = None
            refreshed_count += 1
        else:
            conversation.provider_profile_retry_after = retry_after
            conversation.provider_profile_error_code = result.code[:50]
            status = "unavailable"
            reason_code = result.code
            unavailable_count += 1
        display_name, available, _ = conversation_display_name(
            database_session, conversation
        )
        items.append(MessengerProfileRefreshItem(
            conversation_id=conversation.id,
            status=status,
            contact_display_name=display_name,
            display_name_available=available,
            reason_code=reason_code,
        ))
    database_session.commit()
    return MessengerProfileRefreshData(
        page_display_name=page_display_name(stored_channel),
        page_status=page_status,
        items=items,
        requested_count=len(conversation_ids) + len(cached_items),
        refreshed_count=refreshed_count,
        unavailable_count=unavailable_count,
    )


def calculate_eligibility(
    database_session: Session,
    conversation: WhatsAppConversation,
) -> MessengerEligibilityData:
    inbound_at = _aware(conversation.last_inbound_at)
    expires_at = (
        inbound_at + MESSENGER_REPLY_WINDOW
        if inbound_at is not None
        else None
    )
    restrictions: list[dict[str, str]] = []
    if expires_at is None:
        restrictions.append({
            "code": "messenger_interaction_required",
            "message": "A qualifying incoming Messenger interaction is required before replying",
        })
    elif expires_at <= _now():
        restrictions.append({
            "code": "messenger_reply_window_closed",
            "message": "The standard Messenger reply window is closed",
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
            if restricted.intersection({"messenger", "facebook", "facebook_messenger"}):
                restrictions.append({"code": "messenger_channel_restricted", "message": "The linked customer restricts Messenger communication"})
    return MessengerEligibilityData(
        reply_allowed=not restrictions,
        window_expires_at=expires_at,
        blocking_reasons=restrictions,
    )


def _request_hash(
    *, conversation_id: uuid.UUID, recipient: str, text: str
) -> str:
    canonical = {
        "provider": MESSENGER_PROVIDER,
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
    payload: MessengerReplyRequest,
    provider: MetaMessengerProvider,
) -> MetaMessengerSendResult:
    channel = channel_for_context(
        database_session, context, provider, require_send=True
    )
    conversation = get_messenger_conversation_record(
        database_session, context, conversation_id
    )
    recipient = conversation.external_address
    request_hash = _request_hash(
        conversation_id=conversation.id,
        recipient=recipient,
        text=payload.text,
    )
    existing = get_message_by_idempotency(
        database_session,
        tenant_id=context.tenant.id,
        idempotency_key=payload.idempotency_key,
    )
    if existing is not None:
        if existing.provider != MESSENGER_PROVIDER or existing.request_hash != request_hash:
            raise MetaMessengerIdempotencyConflictError
        get_messenger_conversation_record(
            database_session, context, existing.conversation_id
        )
        return MetaMessengerSendResult(existing, True)

    eligibility = calculate_eligibility(database_session, conversation)
    if not eligibility.reply_allowed:
        reason = eligibility.blocking_reasons[0]
        raise MetaMessengerEligibilityError(
            reason.code,
            reason.message,
            window_expires_at=(
                eligibility.window_expires_at.isoformat()
                if eligibility.window_expires_at else None
            ),
            blocking_reasons=[item.model_dump() for item in eligibility.blocking_reasons],
        )
    if len(payload.text) > settings.meta_messenger_reply_max_characters:
        raise MetaMessengerEligibilityError(
            "message_too_long",
            f"Messenger text exceeds {settings.meta_messenger_reply_max_characters} characters",
        )

    message = WhatsAppMessage(
        tenant_id=context.tenant.id,
        conversation_id=conversation.id,
        direction="outbound",
        provider=MESSENGER_PROVIDER,
        message_mode="text",
        body=payload.text,
        media_metadata=[],
        sender_address=channel.sender_address,
        recipient_address=recipient,
        status="unknown",
        submission_state="processing",
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        requested_by_user_id=context.user.id,
        occurred_at=_now(),
    )
    database_session.add(message)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="meta_messenger.reply.started",
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
            idempotency_key=payload.idempotency_key,
        )
        if existing is not None and existing.provider == MESSENGER_PROVIDER and existing.request_hash == request_hash:
            return MetaMessengerSendResult(existing, True)
        if existing is not None:
            raise MetaMessengerIdempotencyConflictError from exception
        raise

    provider_result: MetaMessengerResult | None = None
    provider_error: Exception | None = None
    try:
        await meta_messenger_reply_rate_limiter.enforce(
            f"{context.tenant.id}:{context.user.id}",
            limit=settings.meta_messenger_reply_rate_limit_requests,
            window_seconds=settings.meta_messenger_reply_rate_limit_window_seconds,
        )
        stored = database_session.get(WhatsAppMessage, message.id)
        if stored is None:
            raise MetaMessengerNotFoundError
        stored.provider_request_started_at = _now()
        database_session.commit()
        provider_result = await provider.send_text(
            to=recipient,
            body=payload.text,
            metadata=str(message.id),
        )
    except RateLimitExceededError:
        provider_error = MetaMessengerRejectedError(
            code="application_rate_limit",
            safe_message="Messenger reply rate limit exceeded",
            http_status=429,
        )
    except Exception as exception:
        provider_error = exception

    for attempt in range(2):
        try:
            stored = database_session.get(WhatsAppMessage, message.id)
            stored_conversation = database_session.get(WhatsAppConversation, conversation.id)
            if stored is None or stored_conversation is None:
                raise MetaMessengerNotFoundError
            completed = _now()
            if provider_result is not None:
                existing_mid = get_message_by_provider_sid(
                    database_session,
                    provider_result.message_id,
                    provider=MESSENGER_PROVIDER,
                )
                if existing_mid is not None and existing_mid.id != stored.id:
                    raise MetaMessengerEligibilityError(
                        "provider_message_conflict",
                        "The provider message identifier is already reconciled",
                    )
                stored.provider_message_sid = provider_result.message_id
                stored.status = provider_result.status
            elif isinstance(provider_error, (MetaMessengerRejectedError, MetaMessengerConfigurationError)):
                stored.status = "failed"
                stored.provider_error_code = provider_error.code
                stored.failure_reason = provider_error.safe_message
            else:
                stored.status = "unknown"
                stored.provider_error_code = "acceptance_unknown"
                stored.failure_reason = "Meta Messenger acceptance could not be determined"
            stored.submission_state = "completed"
            stored.completed_at = completed
            stored_conversation.last_message_at = _max_timestamp(stored_conversation.last_message_at, completed)
            stored_conversation.last_message_preview = stored.body[:240]
            if stored.provider_message_sid:
                for event in list_unmatched_delivery_events(
                    database_session,
                    provider_message_sid=stored.provider_message_sid,
                    provider=MESSENGER_PROVIDER,
                ):
                    event.message_id = stored.id
                    stored.status = _advance_status(stored.status, event.status)
            _add_activity(database_session, conversation=stored_conversation, message=stored)
            database_session.commit()
            database_session.refresh(stored)
            return MetaMessengerSendResult(stored, False)
        except SQLAlchemyError:
            database_session.rollback()
            if attempt == 1:
                raise
    raise RuntimeError("unreachable")


def recover_operation(
    database_session: Session,
    context: WorkspaceContext,
    idempotency_key: str,
) -> MetaMessengerRecoveryResult:
    message = get_message_by_idempotency(
        database_session,
        tenant_id=context.tenant.id,
        idempotency_key=idempotency_key,
        provider=MESSENGER_PROVIDER,
    )
    if message is None:
        return MetaMessengerRecoveryResult(None, "missing")
    get_messenger_conversation_record(database_session, context, message.conversation_id)
    return MetaMessengerRecoveryResult(message, _operation_state(message))


def mark_read(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
):
    conversation = get_messenger_conversation_record(database_session, context, conversation_id)
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
    payload: MessengerConversationLinkRequest,
) -> WhatsAppConversation:
    conversation = get_messenger_conversation_record(
        database_session, context, conversation_id, for_update=True
    )
    if payload.lead_id is not None:
        conditions = [Lead.id == payload.lead_id, Lead.tenant_id == context.tenant.id, Lead.deleted_at.is_(None)]
        if _assigned_scope(context) is not None:
            conditions.append(Lead.assigned_user_id == context.user.id)
        lead = database_session.scalar(select(Lead).where(*conditions))
        if lead is None:
            raise MetaMessengerLinkTargetError
        conversation.lead_id, conversation.customer_id = lead.id, None
    else:
        customer = database_session.scalar(select(Customer).where(
            Customer.id == payload.customer_id, Customer.tenant_id == context.tenant.id
        ))
        if customer is None:
            raise MetaMessengerLinkTargetError
        if _assigned_scope(context) is not None:
            visible = database_session.scalar(select(LeadConversion.id).join(
                Lead, Lead.id == LeadConversion.lead_id
            ).where(
                LeadConversion.customer_id == customer.id,
                LeadConversion.tenant_id == context.tenant.id,
                Lead.assigned_user_id == context.user.id,
            ).limit(1))
            if visible is None:
                raise MetaMessengerLinkTargetError
        conversation.customer_id, conversation.lead_id = customer.id, None
    database_session.flush()
    for message in database_session.scalars(select(WhatsAppMessage).where(
        WhatsAppMessage.conversation_id == conversation.id,
        WhatsAppMessage.provider == MESSENGER_PROVIDER,
    )).all():
        _add_activity(database_session, conversation=conversation, message=message)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="meta_messenger.conversation.linked",
        target_membership_id=context.membership.id,
        details={"conversation_id": str(conversation.id),
                 "lead_id": str(conversation.lead_id) if conversation.lead_id else None,
                 "customer_id": str(conversation.customer_id) if conversation.customer_id else None},
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
    if not MESSENGER_ID_PATTERN.fullmatch(normalized):
        raise MetaMessengerPayloadError
    return normalized


def _attachment_metadata(message: dict[str, Any]) -> list[dict[str, object]]:
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return []
    output: list[dict[str, object]] = []
    for attachment in attachments[:10]:
        if not isinstance(attachment, dict):
            continue
        raw_type = str(attachment.get("type") or "attachment").strip().casefold()
        safe_type = raw_type if raw_type in {"audio", "file", "image", "video"} else "attachment"
        output.append({"type": safe_type, "unsupported": True})
    return output


def _message_body(message: dict[str, Any], media: list[dict[str, object]]) -> str:
    raw_text = message.get("text")
    text = str(raw_text)[:10_000] if isinstance(raw_text, str) else ""
    placeholders = [
        f"[Unsupported Messenger {item['type']} attachment]" for item in media
    ]
    parts = [part for part in [text, *placeholders] if part]
    return "\n".join(parts) or "[Unsupported Messenger message]"


def _event_key(
    *,
    channel_id: uuid.UUID,
    provider_message_id: str,
    status: str,
) -> str:
    return hashlib.sha256(
        "|".join(
            (
                MESSENGER_PROVIDER,
                str(channel_id),
                provider_message_id,
                status,
            )
        ).encode()
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
        select(WhatsAppDeliveryEvent.id).where(
            WhatsAppDeliveryEvent.event_key == key
        )
    ) is not None:
        return False
    message = get_message_by_provider_sid(
        database_session,
        provider_message_id,
        provider=MESSENGER_PROVIDER,
    )
    if message is not None and message.tenant_id != channel.tenant_id:
        raise MetaMessengerWebhookRejectedError
    database_session.add(
        WhatsAppDeliveryEvent(
            tenant_id=channel.tenant_id,
            channel_id=channel.id,
            message_id=message.id if message else None,
            provider=MESSENGER_PROVIDER,
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
    page_id: str,
    sender_id: str,
    event: dict[str, Any],
    message_payload: dict[str, Any],
) -> str:
    provider_message_id = str(message_payload.get("mid") or "").strip()
    if not provider_message_id or len(provider_message_id) > 255:
        raise MetaMessengerPayloadError
    existing = get_message_by_provider_sid(
        database_session,
        provider_message_id,
        provider=MESSENGER_PROVIDER,
    )
    if existing is not None:
        if existing.tenant_id != channel.tenant_id:
            raise MetaMessengerWebhookRejectedError
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
    media = _attachment_metadata(message_payload)
    body = _message_body(message_payload, media)
    message = WhatsAppMessage(
        tenant_id=channel.tenant_id,
        conversation_id=conversation.id,
        direction="inbound",
        provider=MESSENGER_PROVIDER,
        message_mode="text",
        body=body,
        media_metadata=media,
        sender_address=sender_id,
        recipient_address=page_id,
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
    page_id: str,
    recipient_id: str,
    event: dict[str, Any],
    message_payload: dict[str, Any],
) -> str:
    provider_message_id = str(message_payload.get("mid") or "").strip()
    if not provider_message_id or len(provider_message_id) > 255:
        raise MetaMessengerPayloadError
    occurred_at = _timestamp_ms(event.get("timestamp"))
    existing = get_message_by_provider_sid(
        database_session, provider_message_id, provider=MESSENGER_PROVIDER
    )
    if existing is not None:
        if existing.tenant_id != channel.tenant_id:
            raise MetaMessengerWebhookRejectedError
        _persist_event(database_session, channel=channel,
            provider_message_id=provider_message_id, status="sent", occurred_at=occurred_at)
        return "reconciled"

    metadata = str(message_payload.get("metadata") or "").strip()
    local_message: WhatsAppMessage | None = None
    try:
        local_id = uuid.UUID(metadata) if metadata else None
    except ValueError:
        local_id = None
    if local_id is not None:
        candidate = database_session.get(WhatsAppMessage, local_id)
        if (
            candidate is not None
            and candidate.provider == MESSENGER_PROVIDER
            and candidate.tenant_id == channel.tenant_id
            and candidate.direction == "outbound"
            and candidate.recipient_address == recipient_id
        ):
            local_message = candidate
    if local_message is None:
        local_message = database_session.scalar(
            select(WhatsAppMessage)
            .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
            .where(
                WhatsAppMessage.tenant_id == channel.tenant_id,
                WhatsAppMessage.provider == MESSENGER_PROVIDER,
                WhatsAppMessage.direction == "outbound",
                WhatsAppMessage.submission_state == "processing",
                WhatsAppMessage.provider_message_sid.is_(None),
                WhatsAppMessage.recipient_address == recipient_id,
                WhatsAppMessage.body == _message_body(message_payload, []),
                WhatsAppConversation.channel_id == channel.id,
            )
            .order_by(WhatsAppMessage.occurred_at.desc())
            .with_for_update()
            .limit(1)
        )
    if local_message is not None:
        local_message.provider_message_sid = provider_message_id
        local_message.status = _advance_status(local_message.status, "sent")
        _persist_event(database_session, channel=channel,
            provider_message_id=provider_message_id, status="sent", occurred_at=occurred_at)
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
    body = _message_body(message_payload, media)
    external = WhatsAppMessage(
        tenant_id=channel.tenant_id,
        conversation_id=conversation.id,
        direction="outbound",
        provider=MESSENGER_PROVIDER,
        message_mode="text",
        body=body,
        media_metadata=media,
        sender_address=page_id,
        recipient_address=recipient_id,
        provider_message_sid=provider_message_id,
        status="sent",
        submission_state="completed",
        occurred_at=occurred_at,
        completed_at=occurred_at,
    )
    database_session.add(external)
    database_session.flush()
    conversation.last_message_at = _max_timestamp(conversation.last_message_at, occurred_at)
    conversation.last_message_preview = body[:240]
    _persist_event(database_session, channel=channel,
        provider_message_id=provider_message_id, status="sent", occurred_at=occurred_at)
    _add_activity(database_session, conversation=conversation, message=external)
    return "external"


def _persist_watermark_events(
    database_session: Session,
    *,
    channel: WhatsAppChannel,
    contact_id: str,
    status: str,
    watermark: object,
    occurred_at: datetime,
) -> int:
    watermark_at = _timestamp_ms(watermark)
    conversation = get_conversation_by_contact(
        database_session, channel_id=channel.id, external_address=contact_id
    )
    persisted = 0
    matched = False
    if conversation is not None:
        database_session.flush()
        messages = database_session.scalars(
            select(WhatsAppMessage).where(
                WhatsAppMessage.conversation_id == conversation.id,
                WhatsAppMessage.provider == MESSENGER_PROVIDER,
                WhatsAppMessage.direction == "outbound",
                WhatsAppMessage.provider_message_sid.is_not(None),
                WhatsAppMessage.occurred_at <= watermark_at,
            )
        ).all()
        for message in messages:
            matched = True
            if _persist_event(database_session, channel=channel,
                provider_message_id=message.provider_message_sid or "",
                status=status, occurred_at=occurred_at):
                persisted += 1
    if not matched:
        synthetic = f"{status}:{contact_id}:{str(watermark).strip()}"
        if _persist_event(database_session, channel=channel,
            provider_message_id=synthetic[:255], status=status, occurred_at=occurred_at):
            persisted = 1
    return persisted


def _process_messaging_event(
    database_session: Session,
    *,
    channel: WhatsAppChannel,
    page_id: str,
    event: dict[str, Any],
    counts: dict[str, int],
) -> None:
    message_payload = event.get("message")
    delivery = event.get("delivery")
    read = event.get("read")
    if not any(
        isinstance(item, dict) for item in (message_payload, delivery, read)
    ):
        counts["unsupported"] += 1
        return
    sender = event.get("sender")
    recipient = event.get("recipient")
    sender_id = _provider_id(sender.get("id") if isinstance(sender, dict) else None)
    recipient_id = _provider_id(
        recipient.get("id") if isinstance(recipient, dict) else None
    )
    occurred_at = _timestamp_ms(event.get("timestamp"))
    if isinstance(message_payload, dict):
        if message_payload.get("is_echo") is True:
            if sender_id != page_id:
                raise MetaMessengerWebhookRejectedError
            outcome = _persist_outbound_echo(
                database_session,
                channel=channel,
                page_id=page_id,
                recipient_id=recipient_id,
                event=event,
                message_payload=message_payload,
            )
            counts["events"] += 1
            if outcome == "external":
                counts["messages"] += 1
            counts["echoes"] += 1
            return
        if recipient_id != page_id:
            raise MetaMessengerWebhookRejectedError
        outcome = _persist_inbound_message(
            database_session,
            channel=channel,
            page_id=page_id,
            sender_id=sender_id,
            event=event,
            message_payload=message_payload,
        )
        counts["messages" if outcome == "created" else "duplicates"] += 1
        return

    if recipient_id != page_id:
        raise MetaMessengerWebhookRejectedError
    if isinstance(delivery, dict):
        raw_ids = delivery.get("mids")
        provider_ids = (
            [item.strip() for item in raw_ids if isinstance(item, str) and item.strip()]
            if isinstance(raw_ids, list)
            else []
        )
        if not provider_ids and delivery.get("watermark") is not None:
            counts["events"] += _persist_watermark_events(
                database_session, channel=channel, contact_id=sender_id,
                status="delivered", watermark=delivery.get("watermark"),
                occurred_at=occurred_at,
            )
            return
        for provider_message_id in provider_ids:
            if len(provider_message_id) > 255:
                continue
            if _persist_event(
                database_session,
                channel=channel,
                provider_message_id=provider_message_id,
                status="delivered",
                occurred_at=occurred_at,
            ):
                counts["events"] += 1
            else:
                counts["duplicates"] += 1
        return

    if isinstance(read, dict):
        if read.get("watermark") is not None:
            counts["events"] += _persist_watermark_events(
                database_session, channel=channel, contact_id=sender_id,
                status="read", watermark=read.get("watermark"),
                occurred_at=occurred_at,
            )
        return


def process_messenger_webhook_payload(
    database_session: Session,
    provider: MetaMessengerProvider,
    payload: dict[str, Any],
    *,
    _retry_on_conflict: bool = True,
) -> dict[str, int]:
    entries = payload.get("entry")
    if (
        not provider.webhook_configured
        or payload.get("object") != "page"
        or not isinstance(entries, list)
    ):
        raise MetaMessengerWebhookRejectedError
    counts = {
        "messages": 0,
        "events": 0,
        "echoes": 0,
        "duplicates": 0,
        "unsupported": 0,
    }
    for entry in entries:
        if not isinstance(entry, dict):
            raise MetaMessengerPayloadError
        page_id = str(entry.get("id") or "").strip()
        if page_id != provider.page_id:
            raise MetaMessengerWebhookRejectedError
        channel = get_active_messenger_channel_by_page_id(
            database_session,
            page_id=page_id,
        )
        if channel is None:
            raise MetaMessengerWebhookRejectedError
        messaging = entry.get("messaging")
        if isinstance(messaging, list):
            for event in messaging:
                if isinstance(event, dict):
                    _process_messaging_event(
                        database_session,
                        channel=channel,
                        page_id=page_id,
                        event=event,
                        counts=counts,
                    )
                else:
                    counts["unsupported"] += 1
        standby = entry.get("standby")
        if isinstance(standby, list):
            counts["unsupported"] += len(standby)
    try:
        database_session.commit()
    except IntegrityError:
        database_session.rollback()
        if _retry_on_conflict:
            return process_messenger_webhook_payload(
                database_session,
                provider,
                payload,
                _retry_on_conflict=False,
            )
        raise
    return counts
