import asyncio
import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.email_consent import is_email_consent_denied
from app.core.permissions import RoleKey
from app.core.rate_limit import RateLimitExceededError, whatsapp_reply_rate_limiter
from app.core.security import WorkspaceContext
from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead
from app.models.lead_activity import LeadActivity
from app.models.whatsapp import (
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppDeliveryEvent,
    WhatsAppMessage,
)
from app.providers.twilio_whatsapp import (
    TwilioWhatsAppAcceptanceUnknownError,
    TwilioWhatsAppConfigurationError,
    TwilioWhatsAppProvider,
    TwilioWhatsAppRejectedError,
    TwilioWhatsAppResult,
    normalize_whatsapp_address,
    safe_twilio_whatsapp_rejection_message,
)
from app.repositories.whatsapp_repository import (
    ConversationPage,
    MessagePage,
    get_active_channel_by_provider_identity,
    get_channel_for_tenant,
    get_conversation_by_contact,
    get_conversation_for_context,
    get_message_by_idempotency,
    get_message_by_provider_sid,
    list_conversations,
    list_messages,
    list_unmatched_delivery_events,
    mark_conversation_read,
)
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.whatsapp import WhatsAppConversationLinkRequest, WhatsAppReplyRequest


SERVICE_WINDOW = timedelta(hours=24)
UNSUPPORTED_MEDIA_PLACEHOLDER = "[Unsupported WhatsApp media]"
STATUS_RANK = {
    "unknown": -1,
    "accepted": 0,
    "queued": 1,
    "sending": 2,
    "sent": 3,
    "failed": 4,
    "undelivered": 4,
    "delivered": 5,
    "read": 6,
}


class WhatsAppNotFoundError(Exception):
    pass


class WhatsAppChannelNotConfiguredError(Exception):
    pass


class WhatsAppWebhookRejectedError(Exception):
    pass


class WhatsAppIdempotencyConflictError(Exception):
    pass


class WhatsAppServiceWindowClosedError(Exception):
    def __init__(self, expires_at: datetime | None) -> None:
        self.expires_at = expires_at


class WhatsAppEligibilityError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message


class WhatsAppLinkTargetError(Exception):
    pass


@dataclass(frozen=True)
class InboundResult:
    message: WhatsAppMessage
    duplicate: bool


@dataclass(frozen=True)
class ReplyResult:
    message: WhatsAppMessage
    replayed: bool


@dataclass(frozen=True)
class RecoveryResult:
    message: WhatsAppMessage | None
    operation_state: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def service_window(conversation: WhatsAppConversation) -> tuple[bool, datetime | None]:
    inbound = _aware(conversation.last_inbound_at)
    if inbound is None:
        return False, None
    expires_at = inbound + SERVICE_WINDOW
    return _now() < expires_at, expires_at


def conversation_contact_name(
    database_session: Session,
    conversation: WhatsAppConversation,
) -> str | None:
    if conversation.lead_id is not None:
        row = database_session.execute(
            select(Lead.first_name, Lead.last_name).where(
                Lead.id == conversation.lead_id,
                Lead.tenant_id == conversation.tenant_id,
            )
        ).one_or_none()
    elif conversation.customer_id is not None:
        row = database_session.execute(
            select(Customer.first_name, Customer.last_name).where(
                Customer.id == conversation.customer_id,
                Customer.tenant_id == conversation.tenant_id,
            )
        ).one_or_none()
    else:
        row = None
    if row is None:
        return None
    return " ".join(part for part in row if part) or None


def _assigned_scope(context: WorkspaceContext) -> uuid.UUID | None:
    return context.user.id if context.role == RoleKey.STAFF.value else None


def get_conversation_page(
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
        provider="twilio",
    )


def get_conversation_record(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
) -> WhatsAppConversation:
    conversation = get_conversation_for_context(
        database_session,
        tenant_id=context.tenant.id,
        conversation_id=conversation_id,
        assigned_scope_user_id=_assigned_scope(context),
        provider="twilio",
    )
    if conversation is None:
        raise WhatsAppNotFoundError
    return conversation


def get_message_page(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
    *,
    page: int,
    size: int,
) -> tuple[WhatsAppConversation, MessagePage]:
    conversation = get_conversation_record(database_session, context, conversation_id)
    return conversation, list_messages(
        database_session,
        conversation_id=conversation.id,
        page=page,
        size=size,
    )


def _media_metadata(form: dict[str, str]) -> list[dict[str, object]]:
    try:
        count = max(0, min(int(form.get("NumMedia", "0")), 10))
    except ValueError:
        count = 0
    return [
        {
            "index": index,
            "content_type": (form.get(f"MediaContentType{index}") or "")[:120] or None,
            "unsupported": True,
        }
        for index in range(count)
    ]


def _activity_lead_id(
    database_session: Session,
    conversation: WhatsAppConversation,
) -> uuid.UUID | None:
    if conversation.lead_id is not None:
        return conversation.lead_id
    if conversation.customer_id is None:
        return None
    return database_session.scalar(
        select(LeadConversion.lead_id)
        .where(
            LeadConversion.tenant_id == conversation.tenant_id,
            LeadConversion.customer_id == conversation.customer_id,
        )
        .order_by(LeadConversion.converted_at.desc(), LeadConversion.id.desc())
        .limit(1)
    )


def _add_activity(
    database_session: Session,
    *,
    conversation: WhatsAppConversation,
    message: WhatsAppMessage,
) -> None:
    if database_session.scalar(
        select(LeadActivity.id).where(LeadActivity.whatsapp_message_id == message.id)
    ) is not None:
        return
    lead_id = _activity_lead_id(database_session, conversation)
    if lead_id is None:
        return
    lead = database_session.scalar(
        select(Lead).where(Lead.id == lead_id, Lead.tenant_id == conversation.tenant_id)
    )
    if lead is None:
        return
    actor = message.requested_by_user_id
    is_messenger = message.provider == "messenger"
    channel = "other" if is_messenger else "whatsapp"
    inbound_note = (
        "Incoming Facebook Messenger message recorded."
        if is_messenger
        else "Incoming WhatsApp message recorded."
    )
    outbound_note = (
        "System Facebook Messenger submission recorded; see message events for provider status."
        if is_messenger
        else "System WhatsApp reply submission recorded; see message events for provider status."
    )
    database_session.add(
        LeadActivity(
            tenant_id=conversation.tenant_id,
            lead_id=lead.id,
            activity_type="inbound_reply" if message.direction == "inbound" else "follow_up",
            channel=channel,
            direction=message.direction,
            occurred_at=message.occurred_at,
            performed_by=actor,
            outcome=message.status,
            notes=(
                inbound_note
                if message.direction == "inbound"
                else outbound_note
            ),
            recorded_by=actor or lead.created_by,
            is_system=True,
            externally_performed=False,
            provider_verified=bool(message.provider_message_sid),
            whatsapp_message_id=message.id,
        )
    )


def process_inbound_webhook(
    database_session: Session,
    provider: TwilioWhatsAppProvider,
    form: dict[str, str],
) -> InboundResult:
    account_sid = (form.get("AccountSid") or "").strip()
    provider_sid = (form.get("MessageSid") or form.get("SmsMessageSid") or "").strip()
    if not provider_sid or account_sid != provider.account_sid:
        raise WhatsAppWebhookRejectedError
    try:
        sender = normalize_whatsapp_address(form.get("From"))
        receiver = normalize_whatsapp_address(form.get("To"))
    except ValueError as exception:
        raise WhatsAppWebhookRejectedError from exception
    if receiver != provider.from_address:
        raise WhatsAppWebhookRejectedError
    channel = get_active_channel_by_provider_identity(
        database_session,
        account_sid=account_sid,
        sender_address=receiver,
    )
    if channel is None:
        raise WhatsAppWebhookRejectedError
    existing = get_message_by_provider_sid(database_session, provider_sid, provider="twilio")
    if existing is not None:
        if existing.tenant_id != channel.tenant_id:
            raise WhatsAppWebhookRejectedError
        return InboundResult(existing, True)

    now = _now()
    conversation = get_conversation_by_contact(
        database_session,
        channel_id=channel.id,
        external_address=sender,
        for_update=True,
    )
    if conversation is None:
        conversation = WhatsAppConversation(
            tenant_id=channel.tenant_id,
            channel_id=channel.id,
            external_address=sender,
            last_message_at=now,
            last_inbound_at=now,
        )
        database_session.add(conversation)
        database_session.flush()
    media = _media_metadata(form)
    body = form.get("Body") or ""
    if not body and media:
        body = UNSUPPORTED_MEDIA_PLACEHOLDER
    message = WhatsAppMessage(
        tenant_id=channel.tenant_id,
        conversation_id=conversation.id,
        direction="inbound",
        provider="twilio",
        body=body,
        media_metadata=media,
        sender_address=sender,
        recipient_address=receiver,
        provider_message_sid=provider_sid,
        status="received",
        submission_state="received",
        occurred_at=now,
        completed_at=now,
    )
    database_session.add(message)
    database_session.flush()
    conversation.last_message_at = now
    conversation.last_inbound_at = now
    conversation.last_message_preview = body[:240]
    _add_activity(database_session, conversation=conversation, message=message)
    try:
        database_session.commit()
    except IntegrityError:
        database_session.rollback()
        existing = get_message_by_provider_sid(database_session, provider_sid, provider="twilio")
        if existing is not None:
            return InboundResult(existing, True)
        raise
    database_session.refresh(message)
    return InboundResult(message, False)


def _request_hash(conversation_id: uuid.UUID, payload: WhatsAppReplyRequest) -> str:
    canonical = {
        "conversation_id": str(conversation_id),
        "body": payload.body,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _eligibility(
    database_session: Session,
    conversation: WhatsAppConversation,
) -> tuple[str, str] | None:
    if conversation.lead_id is not None:
        lead = database_session.scalar(
            select(Lead).where(
                Lead.id == conversation.lead_id,
                Lead.tenant_id == conversation.tenant_id,
            )
        )
        if lead is None or lead.deleted_at is not None:
            return "lead_unavailable", "The linked lead is unavailable"
        if is_email_consent_denied(lead.consent_status):
            return "messaging_consent_denied", "The linked lead explicitly denied messaging consent"
    if conversation.customer_id is not None:
        customer = database_session.scalar(
            select(Customer).where(
                Customer.id == conversation.customer_id,
                Customer.tenant_id == conversation.tenant_id,
            )
        )
        if customer is None:
            return "customer_unavailable", "The linked customer is unavailable"
        if is_email_consent_denied(customer.communication_consent_status):
            return "messaging_consent_denied", "The linked customer explicitly denied messaging consent"
        if customer.suppressed_at is not None:
            return "customer_suppressed", "The linked customer is suppressed"
        if "whatsapp" in {str(item).casefold() for item in customer.channel_restrictions}:
            return "channel_restricted", "WhatsApp is restricted for the linked customer"
    return None


def _prepare_reply(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
    payload: WhatsAppReplyRequest,
    provider: TwilioWhatsAppProvider,
) -> tuple[WhatsAppMessage, WhatsAppConversation, bool]:
    request_hash = _request_hash(conversation_id, payload)
    existing = get_message_by_idempotency(
        database_session,
        tenant_id=context.tenant.id,
        idempotency_key=payload.idempotency_key,
    )
    if existing is not None:
        if existing.provider != "twilio" or existing.request_hash != request_hash:
            raise WhatsAppIdempotencyConflictError
        conversation = get_conversation_record(
            database_session, context, existing.conversation_id
        )
        return existing, conversation, True

    if not provider.configured or provider.status_callback_url is None:
        raise WhatsAppChannelNotConfiguredError
    channel = get_channel_for_tenant(database_session, tenant_id=context.tenant.id)
    if (
        channel is None
        or channel.twilio_account_sid != provider.account_sid
        or channel.sender_address != provider.from_address
    ):
        raise WhatsAppChannelNotConfiguredError
    conversation = get_conversation_record(database_session, context, conversation_id)
    is_open, expires_at = service_window(conversation)
    if not is_open:
        raise WhatsAppServiceWindowClosedError(expires_at)
    eligibility = _eligibility(database_session, conversation)
    if eligibility:
        raise WhatsAppEligibilityError(*eligibility)
    if len(payload.body) > settings.whatsapp_reply_max_characters:
        raise WhatsAppEligibilityError(
            "message_too_long",
            f"WhatsApp text exceeds {settings.whatsapp_reply_max_characters} characters",
        )

    now = _now()
    message = WhatsAppMessage(
        tenant_id=context.tenant.id,
        conversation_id=conversation.id,
        direction="outbound",
        provider="twilio",
        body=payload.body,
        media_metadata=[],
        sender_address=channel.sender_address,
        recipient_address=conversation.external_address,
        status="unknown",
        submission_state="processing",
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        requested_by_user_id=context.user.id,
        occurred_at=now,
        provider_request_started_at=now,
    )
    database_session.add(message)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="whatsapp.reply.started",
        target_membership_id=context.membership.id,
        details={
            "whatsapp_message_id": str(message.id),
            "conversation_id": str(conversation.id),
        },
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
        if (
            existing is not None
            and existing.provider == "twilio"
            and existing.request_hash == request_hash
        ):
            return existing, conversation, True
        if existing is not None:
            raise WhatsAppIdempotencyConflictError from exception
        raise
    return message, conversation, False


def _advance_status(current: str, incoming: str) -> str:
    if current == "read":
        return current
    if current == "delivered" and incoming not in {"read"}:
        return current
    if current in {"failed", "undelivered"} and incoming not in {"delivered", "read"}:
        return current
    return incoming if STATUS_RANK.get(incoming, -1) >= STATUS_RANK.get(current, -1) else current


def _apply_delivery_event(message: WhatsAppMessage, event: WhatsAppDeliveryEvent) -> None:
    message.status = _advance_status(message.status, event.status)
    if event.provider_error_code:
        message.provider_error_code = event.provider_error_code
        message.failure_reason = event.safe_reason


def _persist_reply_outcome(
    database_session: Session,
    *,
    message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    result: TwilioWhatsAppResult | None,
    error: Exception | None,
) -> WhatsAppMessage:
    message = database_session.get(WhatsAppMessage, message_id)
    conversation = database_session.get(WhatsAppConversation, conversation_id)
    if message is None or conversation is None:
        raise WhatsAppNotFoundError
    now = _now()
    if result is not None:
        message.provider_message_sid = result.sid
        message.status = result.status
        message.provider_error_code = result.error_code
        message.failure_reason = (
            safe_twilio_whatsapp_rejection_message(result.error_code)
            if result.error_code
            else None
        )
    elif isinstance(error, TwilioWhatsAppRejectedError):
        message.status = "failed"
        message.provider_error_code = error.code
        message.failure_reason = error.safe_message
    elif isinstance(error, TwilioWhatsAppConfigurationError):
        message.status = "failed"
        message.provider_error_code = error.code
        message.failure_reason = error.safe_message
    else:
        message.status = "unknown"
        message.provider_error_code = "acceptance_unknown"
        message.failure_reason = "Twilio WhatsApp acceptance could not be determined"
    message.submission_state = "completed"
    message.completed_at = now
    conversation.last_message_at = now
    conversation.last_message_preview = message.body[:240]
    if message.provider_message_sid:
        for event in list_unmatched_delivery_events(
            database_session,
            provider_message_sid=message.provider_message_sid,
            provider="twilio",
        ):
            event.message_id = message.id
            _apply_delivery_event(message, event)
    _add_activity(database_session, conversation=conversation, message=message)
    database_session.commit()
    database_session.refresh(message)
    return message


async def send_reply(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
    payload: WhatsAppReplyRequest,
    provider: TwilioWhatsAppProvider,
) -> ReplyResult:
    message, conversation, replayed = _prepare_reply(
        database_session, context, conversation_id, payload, provider
    )
    if replayed:
        return ReplyResult(message, True)
    try:
        await whatsapp_reply_rate_limiter.enforce(
            f"{context.tenant.id}:{context.user.id}",
            limit=settings.whatsapp_reply_rate_limit_requests,
            window_seconds=settings.whatsapp_reply_rate_limit_window_seconds,
        )
    except RateLimitExceededError as exception:
        message = _persist_reply_outcome(
            database_session,
            message_id=message.id,
            conversation_id=conversation.id,
            result=None,
            error=TwilioWhatsAppRejectedError(
                code="application_rate_limit",
                safe_message="WhatsApp reply rate limit exceeded",
                http_status=429,
            ),
        )
        raise WhatsAppEligibilityError(
            "rate_limit_exceeded", message.failure_reason or "Rate limit exceeded"
        ) from exception

    result: TwilioWhatsAppResult | None = None
    error: Exception | None = None
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                provider.send_text,
                to=conversation.external_address,
                body=payload.body,
            ),
            timeout=settings.whatsapp_reply_timeout_seconds + 1,
        )
    except asyncio.TimeoutError as exception:
        error = TwilioWhatsAppAcceptanceUnknownError()
        error.__cause__ = exception
    except Exception as exception:
        error = exception

    for attempt in range(2):
        try:
            message = _persist_reply_outcome(
                database_session,
                message_id=message.id,
                conversation_id=conversation.id,
                result=result,
                error=error,
            )
            return ReplyResult(message, False)
        except SQLAlchemyError:
            database_session.rollback()
            if attempt == 1:
                raise
    raise RuntimeError("unreachable")


def recover_operation(
    database_session: Session,
    context: WorkspaceContext,
    idempotency_key: str,
) -> RecoveryResult:
    message = get_message_by_idempotency(
        database_session,
        tenant_id=context.tenant.id,
        idempotency_key=idempotency_key,
        provider="twilio",
    )
    if message is None:
        return RecoveryResult(None, "missing")
    conversation = get_conversation_for_context(
        database_session,
        tenant_id=context.tenant.id,
        conversation_id=message.conversation_id,
        assigned_scope_user_id=_assigned_scope(context),
        provider="twilio",
    )
    if conversation is None:
        raise WhatsAppNotFoundError
    state = "in_progress" if message.submission_state == "processing" else "completed"
    return RecoveryResult(message, state)


def mark_read(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
):
    conversation = get_conversation_record(database_session, context, conversation_id)
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
    payload: WhatsAppConversationLinkRequest,
) -> WhatsAppConversation:
    conversation = get_conversation_for_context(
        database_session,
        tenant_id=context.tenant.id,
        conversation_id=conversation_id,
        assigned_scope_user_id=_assigned_scope(context),
        provider="twilio",
        for_update=True,
    )
    if conversation is None:
        raise WhatsAppNotFoundError
    if payload.lead_id is not None:
        conditions = [Lead.id == payload.lead_id, Lead.tenant_id == context.tenant.id]
        if _assigned_scope(context) is not None:
            conditions.append(Lead.assigned_user_id == context.user.id)
        lead = database_session.scalar(select(Lead).where(*conditions))
        if lead is None:
            raise WhatsAppLinkTargetError
        conversation.lead_id = lead.id
        conversation.customer_id = None
    else:
        customer = database_session.scalar(
            select(Customer).where(
                Customer.id == payload.customer_id,
                Customer.tenant_id == context.tenant.id,
            )
        )
        if customer is None:
            raise WhatsAppLinkTargetError
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
                raise WhatsAppLinkTargetError
        conversation.customer_id = customer.id
        conversation.lead_id = None
    database_session.flush()
    for message in database_session.scalars(
        select(WhatsAppMessage)
        .where(WhatsAppMessage.conversation_id == conversation.id)
        .order_by(WhatsAppMessage.occurred_at, WhatsAppMessage.id)
    ).all():
        _add_activity(database_session, conversation=conversation, message=message)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="whatsapp.conversation.linked",
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


def process_status_webhook(
    database_session: Session,
    provider: TwilioWhatsAppProvider,
    form: dict[str, str],
) -> tuple[WhatsAppDeliveryEvent, bool]:
    account_sid = (form.get("AccountSid") or "").strip()
    provider_sid = (form.get("MessageSid") or form.get("SmsMessageSid") or "").strip()
    raw_status = (form.get("MessageStatus") or form.get("SmsStatus") or "").strip().casefold()
    if (form.get("EventType") or "").strip().casefold() == "read":
        raw_status = "read"
    if account_sid != provider.account_sid or not provider_sid or raw_status not in STATUS_RANK:
        raise WhatsAppWebhookRejectedError
    try:
        sender = normalize_whatsapp_address(form.get("From"))
    except ValueError as exception:
        raise WhatsAppWebhookRejectedError from exception
    channel = get_active_channel_by_provider_identity(
        database_session,
        account_sid=account_sid,
        sender_address=sender,
    )
    if channel is None or sender != provider.from_address:
        raise WhatsAppWebhookRejectedError
    error_code = (form.get("ErrorCode") or "").strip() or None
    event_key = hashlib.sha256(
        "|".join((account_sid, provider_sid, raw_status, error_code or "")).encode()
    ).hexdigest()
    existing = database_session.scalar(
        select(WhatsAppDeliveryEvent).where(
            WhatsAppDeliveryEvent.event_key == event_key
        )
    )
    if existing is not None:
        return existing, True
    message = get_message_by_provider_sid(database_session, provider_sid, provider="twilio")
    if message is not None and message.tenant_id != channel.tenant_id:
        raise WhatsAppWebhookRejectedError
    event = WhatsAppDeliveryEvent(
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        message_id=message.id if message else None,
        provider="twilio",
        provider_message_sid=provider_sid,
        event_key=event_key,
        status=raw_status,
        provider_error_code=error_code,
        safe_reason=(
            safe_twilio_whatsapp_rejection_message(error_code)
            if error_code
            else None
        ),
        occurred_at=_now(),
    )
    database_session.add(event)
    if message is not None:
        _apply_delivery_event(message, event)
    try:
        database_session.commit()
    except IntegrityError:
        database_session.rollback()
        existing = database_session.scalar(
            select(WhatsAppDeliveryEvent).where(
                WhatsAppDeliveryEvent.event_key == event_key
            )
        )
        if existing is not None:
            return existing, True
        raise
    database_session.refresh(event)
    return event, False


def page_count(total: int, size: int) -> int:
    return math.ceil(total / size) if total else 0
