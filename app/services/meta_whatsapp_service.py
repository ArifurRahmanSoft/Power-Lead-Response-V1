import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import RoleKey
from app.core.rate_limit import RateLimitExceededError, meta_whatsapp_send_rate_limiter
from app.core.security import WorkspaceContext
from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead
from app.models.whatsapp import (
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppDeliveryEvent,
    WhatsAppMessage,
)
from app.providers.meta_whatsapp import (
    MetaWhatsAppConfigurationError,
    MetaWhatsAppProvider,
    MetaWhatsAppRejectedError,
    MetaWhatsAppResult,
    safe_meta_error,
)
from app.repositories.whatsapp_repository import (
    ConversationPage,
    MessagePage,
    get_active_meta_channel_by_identity,
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
from app.schemas.meta_whatsapp import (
    MetaConversationLinkRequest,
    MetaEligibilityData,
    MetaSendRequest,
    MetaTemplateData,
    MetaTemplateVariable,
)
from app.services.whatsapp_service import (
    _add_activity,
    _advance_status,
    _assigned_scope,
    _eligibility,
    conversation_contact_name,
    service_window,
)
from app.utils.phone import normalize_e164_phone


META_PROVIDER = "meta"
UNSUPPORTED_MEDIA_PLACEHOLDER = "[Unsupported WhatsApp message]"
TEMPLATE_VARIABLE_PATTERN = re.compile(r"{{\s*([0-9]+)\s*}}")
META_STATUS_MAP = {
    "accepted": "accepted",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
}


class MetaWhatsAppNotFoundError(Exception):
    pass


class MetaWhatsAppChannelNotConfiguredError(Exception):
    pass


class MetaWhatsAppWebhookRejectedError(Exception):
    pass


class MetaWhatsAppIdempotencyConflictError(Exception):
    pass


class MetaWhatsAppEligibilityError(Exception):
    def __init__(self, code: str, message: str, **details: object) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


class MetaWhatsAppLinkTargetError(Exception):
    pass


@dataclass(frozen=True)
class MetaSendResult:
    message: WhatsAppMessage
    replayed: bool


@dataclass(frozen=True)
class MetaRecoveryResult:
    message: WhatsAppMessage | None
    operation_state: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(value)), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return _now()


def _meta_address(value: object) -> str:
    raw = str(value or "").strip()
    if raw and not raw.startswith("+"):
        raw = f"+{raw}"
    return normalize_e164_phone(raw)


def channel_for_context(
    database_session: Session,
    context: WorkspaceContext,
    provider: MetaWhatsAppProvider,
) -> WhatsAppChannel:
    if not provider.configured:
        raise MetaWhatsAppChannelNotConfiguredError
    channel = get_channel_for_tenant(
        database_session, tenant_id=context.tenant.id, provider=META_PROVIDER
    )
    if (
        channel is None
        or channel.provider_account_id != provider.waba_id
        or channel.provider_channel_id != provider.phone_number_id
    ):
        raise MetaWhatsAppChannelNotConfiguredError
    return channel


def get_meta_conversation_record(
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
        provider=META_PROVIDER,
        for_update=for_update,
    )
    if conversation is None:
        raise MetaWhatsAppNotFoundError
    return conversation


def get_meta_conversation_page(
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
        provider=META_PROVIDER,
    )


def get_meta_message_page(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
    *,
    page: int,
    size: int,
) -> tuple[WhatsAppConversation, MessagePage]:
    conversation = get_meta_conversation_record(
        database_session, context, conversation_id
    )
    return conversation, list_messages(
        database_session, conversation_id=conversation.id, page=page, size=size
    )


def _explicit_opt_in(database_session: Session, conversation: WhatsAppConversation) -> bool:
    if conversation.lead_id is not None:
        value = database_session.scalar(
            select(Lead.consent_status).where(
                Lead.id == conversation.lead_id,
                Lead.tenant_id == conversation.tenant_id,
            )
        )
        return str(value or "").strip().casefold() == "granted"
    if conversation.customer_id is not None:
        value = database_session.scalar(
            select(Customer.communication_consent_status).where(
                Customer.id == conversation.customer_id,
                Customer.tenant_id == conversation.tenant_id,
            )
        )
        return str(value or "").strip().casefold() == "granted"
    return False


def calculate_eligibility(
    database_session: Session,
    conversation: WhatsAppConversation,
) -> MetaEligibilityData:
    free_form, expires_at = service_window(conversation)
    restrictions: list[dict[str, str]] = []
    linked_restriction = _eligibility(database_session, conversation)
    if linked_restriction:
        restrictions.append(
            {"code": linked_restriction[0], "message": linked_restriction[1]}
        )
    opt_in = _explicit_opt_in(database_session, conversation)
    if not free_form and not opt_in:
        restrictions.append(
            {
                "code": "whatsapp_opt_in_required",
                "message": (
                    "Business-initiated WhatsApp outreach requires recorded opt-in; "
                    "unknown or empty general consent is not sufficient"
                ),
            }
        )
    blocked = bool(linked_restriction)
    return MetaEligibilityData(
        free_form_allowed=free_form and not blocked,
        service_window_expires_at=expires_at,
        approved_template_required=not free_form,
        template_outreach_allowed=(free_form or opt_in) and not blocked,
        restrictions=restrictions,
    )


def open_conversation(
    database_session: Session,
    context: WorkspaceContext,
    provider: MetaWhatsAppProvider,
    phone_number: str,
) -> WhatsAppConversation:
    channel = channel_for_context(database_session, context, provider)
    try:
        address = normalize_e164_phone(phone_number)
    except ValueError as exception:
        raise MetaWhatsAppEligibilityError(
            "invalid_phone_number",
            "Phone number must use unambiguous international E.164 format",
        ) from exception
    conversation = get_conversation_by_contact(
        database_session, channel_id=channel.id, external_address=address
    )
    if conversation is not None:
        return get_meta_conversation_record(database_session, context, conversation.id)
    if context.role == RoleKey.STAFF.value:
        raise MetaWhatsAppEligibilityError(
            "record_scope_required",
            "Staff may open only conversations linked to an assigned record",
        )
    conversation = WhatsAppConversation(
        tenant_id=context.tenant.id,
        channel_id=channel.id,
        external_address=address,
        last_message_at=_now(),
        last_message_preview=None,
    )
    database_session.add(conversation)
    database_session.commit()
    database_session.refresh(conversation)
    return conversation


def _required_template_variables(template: dict[str, Any]) -> list[MetaTemplateVariable]:
    result: list[MetaTemplateVariable] = []
    for component in template.get("components", []):
        if not isinstance(component, dict):
            continue
        component_type = str(component.get("type") or "").casefold()
        text = str(component.get("text") or "")
        for raw_index in TEMPLATE_VARIABLE_PATTERN.findall(text):
            result.append(
                MetaTemplateVariable(component=component_type, index=int(raw_index))
            )
    return sorted(result, key=lambda item: (item.component, item.index))


def normalize_templates(raw_templates: list[dict[str, Any]]) -> list[MetaTemplateData]:
    output: list[MetaTemplateData] = []
    for item in raw_templates:
        name = str(item.get("name") or "").strip()
        language = str(item.get("language") or "").strip()
        status = str(item.get("status") or "UNKNOWN").upper()
        if not name or not language:
            continue
        output.append(
            MetaTemplateData(
                id=str(item.get("id")) if item.get("id") is not None else None,
                name=name,
                language=language,
                category=(str(item.get("category")) if item.get("category") else None),
                provider_status=status,
                usable=status == "APPROVED",
                components=[
                    component
                    for component in item.get("components", [])
                    if isinstance(component, dict)
                ],
                required_variables=_required_template_variables(item),
            )
        )
    return sorted(output, key=lambda item: (item.name.casefold(), item.language.casefold()))


def _template_components_and_snapshot(
    template: MetaTemplateData,
    payload: MetaSendRequest,
) -> tuple[list[dict[str, Any]], dict[str, object], str]:
    assert payload.template is not None
    expected = {(item.component, item.index) for item in template.required_variables}
    supplied_items = payload.template.parameters
    supplied = {(item.component, item.index): item.value for item in supplied_items}
    if len(supplied) != len(supplied_items):
        raise MetaWhatsAppEligibilityError(
            "duplicate_template_parameter", "Template parameters must be unique"
        )
    if set(supplied) != expected:
        missing = sorted(f"{part}:{index}" for part, index in expected - set(supplied))
        unknown = sorted(f"{part}:{index}" for part, index in set(supplied) - expected)
        raise MetaWhatsAppEligibilityError(
            "invalid_template_parameters",
            "Template parameters do not match the approved provider definition",
            missing=missing,
            unknown=unknown,
        )
    grouped: dict[str, list[tuple[int, str]]] = {}
    for (component, index), value in supplied.items():
        grouped.setdefault(component, []).append((index, value))
    components = [
        {
            "type": component,
            "parameters": [
                {"type": "text", "text": value}
                for _, value in sorted(values, key=lambda entry: entry[0])
            ],
        }
        for component, values in sorted(grouped.items())
    ]
    rendered_parts: list[str] = []
    for raw_component in template.components:
        component_type = str(raw_component.get("type") or "").casefold()
        text = str(raw_component.get("text") or "")
        if not text:
            continue
        rendered = TEMPLATE_VARIABLE_PATTERN.sub(
            lambda match: supplied.get((component_type, int(match.group(1))), ""), text
        )
        rendered_parts.append(rendered)
    snapshot: dict[str, object] = {
        "id": template.id,
        "name": template.name,
        "language": template.language,
        "provider_status": template.provider_status,
        "components": template.components,
        "parameters": [item.model_dump() for item in supplied_items],
    }
    return components, snapshot, "\n".join(rendered_parts) or f"[{template.name}]"


def _purpose_eligibility(
    database_session: Session,
    conversation: WhatsAppConversation,
    purpose: str,
) -> None:
    converted = False
    if conversation.customer_id is not None:
        converted = True
    elif conversation.lead_id is not None:
        converted = database_session.scalar(
            select(LeadConversion.id).where(
                LeadConversion.tenant_id == conversation.tenant_id,
                LeadConversion.lead_id == conversation.lead_id,
            )
        ) is not None
    if purpose == "lead_acquisition" and converted:
        raise MetaWhatsAppEligibilityError(
            "customer_purpose_required",
            "Lead-acquisition messages are not allowed for converted customers",
        )
    if purpose == "customer_service" and conversation.customer_id is None:
        raise MetaWhatsAppEligibilityError(
            "customer_context_required",
            "Customer-service messages require an authorized customer context",
        )


def _request_hash(
    *,
    conversation_id: uuid.UUID,
    recipient: str,
    payload: MetaSendRequest,
) -> str:
    canonical = {
        "provider": META_PROVIDER,
        "conversation_id": str(conversation_id),
        "recipient": recipient,
        "mode": payload.mode,
        "text": payload.text,
        "template": payload.template.model_dump(mode="json") if payload.template else None,
        "purpose": payload.purpose,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _select_template(
    provider: MetaWhatsAppProvider,
    payload: MetaSendRequest,
) -> MetaTemplateData:
    assert payload.template is not None
    templates = normalize_templates(
        await provider.list_templates(limit=settings.meta_whatsapp_template_fetch_limit)
    )
    match = next(
        (
            item
            for item in templates
            if item.name == payload.template.name
            and item.language == payload.template.language
        ),
        None,
    )
    if match is None:
        raise MetaWhatsAppEligibilityError(
            "template_not_found",
            "The selected template and language do not belong to the configured WABA",
        )
    if not match.usable:
        raise MetaWhatsAppEligibilityError(
            "template_not_approved",
            f"The selected Meta template is not usable (status: {match.provider_status})",
        )
    return match


def _resolve_send_conversation(
    database_session: Session,
    context: WorkspaceContext,
    provider: MetaWhatsAppProvider,
    payload: MetaSendRequest,
) -> WhatsAppConversation:
    if payload.conversation_id is not None:
        conversation = get_meta_conversation_record(
            database_session, context, payload.conversation_id
        )
        if payload.phone_number is not None:
            try:
                requested = normalize_e164_phone(payload.phone_number)
            except ValueError as exception:
                raise MetaWhatsAppEligibilityError(
                    "invalid_phone_number",
                    "Phone number must use unambiguous international E.164 format",
                ) from exception
            if requested != conversation.external_address:
                raise MetaWhatsAppEligibilityError(
                    "recipient_mismatch",
                    "The phone number does not match the authorized conversation",
                )
        return conversation
    assert payload.phone_number is not None
    return open_conversation(
        database_session, context, provider, payload.phone_number
    )


async def send_message(
    database_session: Session,
    context: WorkspaceContext,
    payload: MetaSendRequest,
    provider: MetaWhatsAppProvider,
) -> MetaSendResult:
    channel = channel_for_context(database_session, context, provider)
    conversation = _resolve_send_conversation(
        database_session, context, provider, payload
    )
    recipient = conversation.external_address
    request_hash = _request_hash(
        conversation_id=conversation.id, recipient=recipient, payload=payload
    )
    existing = get_message_by_idempotency(
        database_session,
        tenant_id=context.tenant.id,
        idempotency_key=payload.idempotency_key,
    )
    if existing is not None:
        if existing.provider != META_PROVIDER or existing.request_hash != request_hash:
            raise MetaWhatsAppIdempotencyConflictError
        get_meta_conversation_record(database_session, context, existing.conversation_id)
        return MetaSendResult(existing, True)

    _purpose_eligibility(database_session, conversation, payload.purpose)
    linked_restriction = _eligibility(database_session, conversation)
    if linked_restriction:
        raise MetaWhatsAppEligibilityError(*linked_restriction)
    free_form, expires_at = service_window(conversation)
    template_components: list[dict[str, Any]] = []
    template_snapshot: dict[str, object] | None = None
    if payload.mode == "text":
        if not free_form:
            raise MetaWhatsAppEligibilityError(
                "approved_template_required",
                "The customer-service window is closed; use an approved Meta template",
                service_window_expires_at=(expires_at.isoformat() if expires_at else None),
            )
        body = payload.text or ""
        if len(body) > settings.whatsapp_reply_max_characters:
            raise MetaWhatsAppEligibilityError(
                "message_too_long",
                f"WhatsApp text exceeds {settings.whatsapp_reply_max_characters} characters",
            )
    else:
        if not free_form and not _explicit_opt_in(database_session, conversation):
            raise MetaWhatsAppEligibilityError(
                "whatsapp_opt_in_required",
                "Business-initiated WhatsApp templates require recorded opt-in",
            )
        template = await _select_template(provider, payload)
        template_components, template_snapshot, body = _template_components_and_snapshot(
            template, payload
        )

    now = _now()
    message = WhatsAppMessage(
        tenant_id=context.tenant.id,
        conversation_id=conversation.id,
        direction="outbound",
        provider=META_PROVIDER,
        message_mode=payload.mode,
        purpose=payload.purpose,
        template_snapshot=template_snapshot,
        body=body,
        media_metadata=[],
        sender_address=channel.sender_address,
        recipient_address=recipient,
        status="unknown",
        submission_state="processing",
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        requested_by_user_id=context.user.id,
        occurred_at=now,
    )
    database_session.add(message)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="meta_whatsapp.send.started",
        target_membership_id=context.membership.id,
        details={
            "whatsapp_message_id": str(message.id),
            "conversation_id": str(conversation.id),
            "mode": payload.mode,
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
            and existing.provider == META_PROVIDER
            and existing.request_hash == request_hash
        ):
            return MetaSendResult(existing, True)
        if existing is not None:
            raise MetaWhatsAppIdempotencyConflictError from exception
        raise

    provider_result: MetaWhatsAppResult | None = None
    provider_error: Exception | None = None
    try:
        await meta_whatsapp_send_rate_limiter.enforce(
            f"{context.tenant.id}:{context.user.id}",
            limit=settings.meta_whatsapp_send_rate_limit_requests,
            window_seconds=settings.meta_whatsapp_send_rate_limit_window_seconds,
        )
        message.provider_request_started_at = _now()
        database_session.commit()
        if payload.mode == "text":
            provider_result = await provider.send_text(to=recipient, body=body)
        else:
            assert payload.template is not None
            provider_result = await provider.send_template(
                to=recipient,
                name=payload.template.name,
                language=payload.template.language,
                components=template_components,
            )
    except RateLimitExceededError:
        provider_error = MetaWhatsAppRejectedError(
            code="application_rate_limit",
            safe_message="Meta WhatsApp send rate limit exceeded",
            http_status=429,
        )
    except Exception as exception:
        provider_error = exception

    for attempt in range(2):
        try:
            stored = database_session.get(WhatsAppMessage, message.id)
            stored_conversation = database_session.get(
                WhatsAppConversation, conversation.id
            )
            if stored is None or stored_conversation is None:
                raise MetaWhatsAppNotFoundError
            completed = _now()
            if provider_result is not None:
                stored.provider_message_sid = provider_result.message_id
                stored.status = provider_result.status
            elif isinstance(
                provider_error,
                (MetaWhatsAppRejectedError, MetaWhatsAppConfigurationError),
            ):
                stored.status = "failed"
                stored.provider_error_code = provider_error.code
                stored.failure_reason = provider_error.safe_message
            else:
                stored.status = "unknown"
                stored.provider_error_code = "acceptance_unknown"
                stored.failure_reason = "Meta WhatsApp acceptance could not be determined"
            stored.submission_state = "completed"
            stored.completed_at = completed
            stored_conversation.last_message_at = completed
            stored_conversation.last_message_preview = stored.body[:240]
            if stored.provider_message_sid:
                for event in list_unmatched_delivery_events(
                    database_session,
                    provider_message_sid=stored.provider_message_sid,
                    provider=META_PROVIDER,
                ):
                    event.message_id = stored.id
                    stored.status = _advance_status(stored.status, event.status)
                    if event.provider_error_code:
                        stored.provider_error_code = event.provider_error_code
                        stored.failure_reason = event.safe_reason
            _add_activity(
                database_session, conversation=stored_conversation, message=stored
            )
            database_session.commit()
            database_session.refresh(stored)
            return MetaSendResult(stored, False)
        except SQLAlchemyError:
            database_session.rollback()
            if attempt == 1:
                raise
    raise RuntimeError("unreachable")


def recover_operation(
    database_session: Session,
    context: WorkspaceContext,
    idempotency_key: str,
) -> MetaRecoveryResult:
    message = get_message_by_idempotency(
        database_session,
        tenant_id=context.tenant.id,
        idempotency_key=idempotency_key,
        provider=META_PROVIDER,
    )
    if message is None:
        return MetaRecoveryResult(None, "missing")
    get_meta_conversation_record(database_session, context, message.conversation_id)
    state = "in_progress" if message.submission_state == "processing" else "completed"
    return MetaRecoveryResult(message, state)


def mark_read(
    database_session: Session,
    context: WorkspaceContext,
    conversation_id: uuid.UUID,
):
    conversation = get_meta_conversation_record(
        database_session, context, conversation_id
    )
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
    payload: MetaConversationLinkRequest,
) -> WhatsAppConversation:
    conversation = get_meta_conversation_record(
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
            raise MetaWhatsAppLinkTargetError
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
            raise MetaWhatsAppLinkTargetError
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
                raise MetaWhatsAppLinkTargetError
        conversation.customer_id = customer.id
        conversation.lead_id = None
    database_session.flush()
    for message in database_session.scalars(
        select(WhatsAppMessage).where(
            WhatsAppMessage.conversation_id == conversation.id,
            WhatsAppMessage.provider == META_PROVIDER,
        )
    ).all():
        _add_activity(database_session, conversation=conversation, message=message)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="meta_whatsapp.conversation.linked",
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


def _meta_media(message: dict[str, Any]) -> list[dict[str, object]]:
    message_type = str(message.get("type") or "unknown").casefold()
    if message_type == "text":
        return []
    payload = message.get(message_type)
    provider_media_id = payload.get("id") if isinstance(payload, dict) else None
    return [
        {
            "type": message_type[:50],
            "provider_media_id": (
                str(provider_media_id)[:255] if provider_media_id else None
            ),
            "unsupported": True,
        }
    ]


def _inbound_body(message: dict[str, Any]) -> str:
    if str(message.get("type") or "").casefold() == "text":
        text_payload = message.get("text")
        if isinstance(text_payload, dict):
            return str(text_payload.get("body") or "")[:4096]
    message_type = str(message.get("type") or "unknown")[:50]
    return f"[Unsupported WhatsApp {message_type} message]"


def _event_key(
    *, channel_id: uuid.UUID, message_id: str, status: str, occurred_at: datetime, code: str | None
) -> str:
    return hashlib.sha256(
        "|".join(
            (
                META_PROVIDER,
                str(channel_id),
                message_id,
                status,
                occurred_at.isoformat(),
                code or "",
            )
        ).encode()
    ).hexdigest()


def process_webhook_payload(
    database_session: Session,
    provider: MetaWhatsAppProvider,
    payload: dict[str, Any],
    *,
    _retry_on_conflict: bool = True,
) -> dict[str, int]:
    if not provider.configured:
        raise MetaWhatsAppWebhookRejectedError
    entries = payload.get("entry")
    if payload.get("object") != "whatsapp_business_account" or not isinstance(entries, list):
        raise MetaWhatsAppWebhookRejectedError
    counts = {"messages": 0, "statuses": 0, "duplicates": 0}
    for entry in entries:
        if not isinstance(entry, dict) or str(entry.get("id") or "") != provider.waba_id:
            raise MetaWhatsAppWebhookRejectedError
        for change in entry.get("changes", []):
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            phone_number_id = (
                str(metadata.get("phone_number_id") or "")
                if isinstance(metadata, dict)
                else ""
            )
            if phone_number_id != provider.phone_number_id:
                raise MetaWhatsAppWebhookRejectedError
            channel = get_active_meta_channel_by_identity(
                database_session,
                waba_id=provider.waba_id,
                phone_number_id=phone_number_id,
            )
            if channel is None:
                raise MetaWhatsAppWebhookRejectedError
            for raw_message in value.get("messages", []):
                if not isinstance(raw_message, dict):
                    continue
                provider_message_id = str(raw_message.get("id") or "").strip()
                try:
                    sender = _meta_address(raw_message.get("from"))
                except ValueError as exception:
                    raise MetaWhatsAppWebhookRejectedError from exception
                if not provider_message_id:
                    raise MetaWhatsAppWebhookRejectedError
                existing = get_message_by_provider_sid(
                    database_session,
                    provider_message_id,
                    provider=META_PROVIDER,
                )
                if existing is not None:
                    if existing.tenant_id != channel.tenant_id:
                        raise MetaWhatsAppWebhookRejectedError
                    counts["duplicates"] += 1
                    continue
                occurred_at = _timestamp(raw_message.get("timestamp"))
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
                        last_message_at=occurred_at,
                        last_inbound_at=occurred_at,
                    )
                    database_session.add(conversation)
                    database_session.flush()
                body = _inbound_body(raw_message)
                message = WhatsAppMessage(
                    tenant_id=channel.tenant_id,
                    conversation_id=conversation.id,
                    direction="inbound",
                    provider=META_PROVIDER,
                    message_mode="text",
                    body=body,
                    media_metadata=_meta_media(raw_message),
                    sender_address=sender,
                    recipient_address=channel.sender_address,
                    provider_message_sid=provider_message_id,
                    status="received",
                    submission_state="received",
                    occurred_at=occurred_at,
                    completed_at=occurred_at,
                )
                database_session.add(message)
                database_session.flush()
                conversation.last_message_at = max(
                    conversation.last_message_at, occurred_at
                )
                if conversation.last_inbound_at is None or occurred_at > conversation.last_inbound_at:
                    conversation.last_inbound_at = occurred_at
                conversation.last_message_preview = body[:240]
                _add_activity(database_session, conversation=conversation, message=message)
                counts["messages"] += 1
            for raw_status in value.get("statuses", []):
                if not isinstance(raw_status, dict):
                    continue
                provider_message_id = str(raw_status.get("id") or "").strip()
                status = META_STATUS_MAP.get(
                    str(raw_status.get("status") or "").casefold()
                )
                if not provider_message_id or status is None:
                    continue
                occurred_at = _timestamp(raw_status.get("timestamp"))
                error_item = next(
                    (
                        item
                        for item in raw_status.get("errors", [])
                        if isinstance(item, dict)
                    ),
                    None,
                )
                code = (
                    str(error_item.get("code"))
                    if error_item and error_item.get("code") is not None
                    else None
                )
                event_key = _event_key(
                    channel_id=channel.id,
                    message_id=provider_message_id,
                    status=status,
                    occurred_at=occurred_at,
                    code=code,
                )
                existing_event = database_session.scalar(
                    select(WhatsAppDeliveryEvent).where(
                        WhatsAppDeliveryEvent.event_key == event_key
                    )
                )
                if existing_event is not None:
                    counts["duplicates"] += 1
                    continue
                message = get_message_by_provider_sid(
                    database_session,
                    provider_message_id,
                    provider=META_PROVIDER,
                )
                if message is not None and message.tenant_id != channel.tenant_id:
                    raise MetaWhatsAppWebhookRejectedError
                event = WhatsAppDeliveryEvent(
                    tenant_id=channel.tenant_id,
                    channel_id=channel.id,
                    message_id=message.id if message else None,
                    provider=META_PROVIDER,
                    provider_message_sid=provider_message_id,
                    event_key=event_key,
                    status=status,
                    provider_error_code=code,
                    safe_reason=safe_meta_error(code) if code else None,
                    occurred_at=occurred_at,
                )
                database_session.add(event)
                if message is not None:
                    message.status = _advance_status(message.status, status)
                    if code:
                        message.provider_error_code = code
                        message.failure_reason = event.safe_reason
                counts["statuses"] += 1
    try:
        database_session.commit()
    except IntegrityError:
        database_session.rollback()
        # Re-read once after a concurrent unique-key winner. This makes the
        # winner visible while still processing any other events in the same
        # payload; a second race is surfaced so Meta retries the webhook.
        if _retry_on_conflict:
            return process_webhook_payload(
                database_session,
                provider,
                payload,
                _retry_on_conflict=False,
            )
        raise
    return counts


def page_count(total: int, size: int) -> int:
    return math.ceil(total / size) if total else 0
