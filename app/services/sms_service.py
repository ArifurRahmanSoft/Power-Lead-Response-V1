import asyncio
import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.email_consent import is_email_consent_denied, normalize_email_consent_status
from app.core.lead_catalogs import LeadStatus
from app.core.permissions import RoleKey
from app.core.security import WorkspaceContext
from app.models.lead import Lead
from app.models.lead_activity import LeadActivity
from app.models.sms_message import SmsBatch, SmsBatchRecipient, SmsMessage
from app.providers.twilio_sms import (
    TwilioSmsAcceptanceUnknownError,
    TwilioSmsConfigurationError,
    TwilioSmsError,
    TwilioSmsPreflightError,
    TwilioSmsProviderFailureError,
    TwilioSmsProvider,
    TwilioSmsRejectedError,
    TwilioSmsResult,
    safe_twilio_rejection_message,
)
from app.repositories.lead_repository import get_lead
from app.repositories.message_template_repository import get_message_template
from app.repositories.sms_repository import (
    add_message_event,
    get_batch_by_idempotency,
    get_sms_batch,
    get_sms_message,
    list_batch_messages,
    list_batch_recipients,
    list_message_events,
)
from app.repositories.workspace_repository import add_authorization_audit, lock_tenant
from app.schemas.sms import SmsSendRequest
from app.utils.mail_template import lead_placeholder_values, render_text_template
from app.utils.phone import normalize_e164_phone

SMS_MAX_BODY_CHARACTERS = 1600


class SmsBatchLimitError(Exception):
    pass


class SmsTemplateNotFoundError(Exception):
    pass


class SmsIdempotencyConflictError(Exception):
    pass


class SmsBatchNotFoundError(Exception):
    pass


class SmsMessageNotFoundError(Exception):
    pass


class SmsStatusRefreshError(Exception):
    pass


@dataclass(frozen=True)
class SmsRefreshResult:
    message: SmsMessage
    refreshed: bool
    next_refresh_at: datetime | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _request_hash(payload: SmsSendRequest) -> str:
    canonical = {
        "lead_ids": [str(value) for value in payload.lead_ids],
        "message_template_id": str(payload.message_template_id),
        "contact_type": payload.contact_type.value,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _scope_allows(context: WorkspaceContext, lead: Lead) -> bool:
    return not (
        context.role == RoleKey.STAFF.value
        and lead.assigned_user_id != context.user.id
    )


def _consent_reason(value: object) -> tuple[str, str] | None:
    if not is_email_consent_denied(value):
        return None
    normalized = normalize_email_consent_status(value)
    if normalized == "withdrawn":
        return "sms_consent_withdrawn", "Lead messaging consent was withdrawn"
    return "sms_consent_denied", "Lead messaging consent is explicitly denied"


def _eligibility(lead: Lead, expected_phone: str | None = None) -> tuple[str, str] | None:
    if lead.deleted_at is not None:
        return "lead_archived", "Archived leads cannot receive SMS messages"
    if lead.status == LeadStatus.CONVERTED.value:
        return "lead_converted", "Lead-acquisition SMS is not allowed for converted customers"
    if lead.status == LeadStatus.DISQUALIFIED.value:
        return "lead_disqualified", "Disqualified leads cannot receive SMS messages"
    consent = _consent_reason(lead.consent_status)
    if consent:
        return consent
    try:
        phone = normalize_e164_phone(lead.phone)
    except ValueError as exception:
        return "invalid_phone", str(exception)
    if expected_phone is not None and phone != expected_phone:
        return "phone_changed", "Lead phone changed after the SMS request was prepared"
    return None


def _mark_state(message: SmsMessage, status: str, now: datetime) -> None:
    message.status = status
    if status in {"accepted", "queued", "sent", "delivered"} and message.accepted_at is None:
        message.accepted_at = now
    if status in {"sent", "delivered"} and message.sent_at is None:
        message.sent_at = now
    if status == "delivered":
        message.delivered_at = now
    if status in {"failed", "undelivered"}:
        message.failed_at = now


def _add_activity(
    database_session: Session,
    context: WorkspaceContext,
    message: SmsMessage,
    now: datetime,
) -> None:
    existing = database_session.scalar(
        select(LeadActivity.id).where(LeadActivity.sms_message_id == message.id)
    )
    if existing is not None:
        return
    database_session.add(
        LeadActivity(
            tenant_id=context.tenant.id,
            lead_id=message.lead_id,
            activity_type=(
                "initial_contact"
                if message.contact_type == "initial"
                else "follow_up"
            ),
            channel="sms",
            direction="outbound",
            occurred_at=now,
            performed_by=context.user.id,
            outcome=message.status,
            notes="System SMS submission recorded; see SMS message events for provider status.",
            recorded_by=context.user.id,
            is_system=True,
            externally_performed=False,
            provider_verified=message.provider_message_sid is not None,
            sms_message_id=message.id,
            contact_stage=message.contact_type,
        )
    )


def _sync_recipient(database_session: Session, message: SmsMessage) -> None:
    recipient = database_session.scalar(
        select(SmsBatchRecipient).where(
            SmsBatchRecipient.sms_message_id == message.id
        )
    )
    if recipient is None:
        return
    recipient.status = message.status
    recipient.reason_code = message.provider_error_code
    recipient.reason = message.failure_reason


def _finish_batch(database_session: Session, batch: SmsBatch) -> None:
    statuses = [item.status for item in list_batch_recipients(database_session, batch.id)]
    completed = {"accepted", "queued", "sent", "delivered"}
    batch.status = (
        "completed"
        if statuses and all(value in completed for value in statuses)
        else "partially_completed"
    )
    batch.completed_at = _now()


def _existing_batch(
    database_session: Session,
    context: WorkspaceContext,
    payload: SmsSendRequest,
) -> SmsBatch | None:
    existing = get_batch_by_idempotency(
        database_session, context.tenant.id, payload.idempotency_key
    )
    if (
        existing is not None
        and context.role == RoleKey.STAFF.value
        and existing.created_by_user_id != context.user.id
    ):
        raise SmsIdempotencyConflictError
    if existing is not None and existing.request_hash != _request_hash(payload):
        raise SmsIdempotencyConflictError
    return existing


def prepare_sms_batch(
    database_session: Session,
    context: WorkspaceContext,
    payload: SmsSendRequest,
    provider: TwilioSmsProvider,
) -> tuple[SmsBatch, bool]:
    if len(payload.lead_ids) > settings.sms_direct_send_max_recipients:
        raise SmsBatchLimitError
    existing = _existing_batch(database_session, context, payload)
    if existing is not None:
        return existing, True

    lock_tenant(database_session, context.tenant.id)
    existing = _existing_batch(database_session, context, payload)
    if existing is not None:
        database_session.rollback()
        return existing, True

    template = get_message_template(
        database_session,
        context.tenant.id,
        payload.message_template_id,
    )
    if template is None:
        database_session.rollback()
        raise SmsTemplateNotFoundError

    batch = SmsBatch(
        tenant_id=context.tenant.id,
        message_template_id=template.id,
        template_updated_at=template.updated_at,
        contact_type=payload.contact_type.value,
        idempotency_key=payload.idempotency_key,
        request_hash=_request_hash(payload),
        selection_count=len(payload.lead_ids),
        created_by_user_id=context.user.id,
    )
    database_session.add(batch)
    database_session.flush()

    prepared: list[tuple[int, uuid.UUID, Lead, str, str]] = []
    completed_recipient_ids: set[uuid.UUID] = set()
    for ordinal, requested_id in enumerate(payload.lead_ids):
        lead = get_lead(
            database_session,
            context.tenant.id,
            requested_id,
            include_archived=True,
        )
        if lead is None or not _scope_allows(context, lead):
            database_session.add(
                SmsBatchRecipient(
                    tenant_id=context.tenant.id,
                    batch_id=batch.id,
                    requested_lead_id=requested_id,
                    ordinal=ordinal,
                    status="skipped",
                    reason_code="lead_unavailable",
                    reason="Lead is unavailable or outside the authorized scope",
                    warnings=[],
                )
            )
            completed_recipient_ids.add(requested_id)
            continue
        eligibility = _eligibility(lead)
        rendered, missing = render_text_template(
            template.body, lead_placeholder_values(lead), escape_html=False
        )
        if missing:
            eligibility = (
                "missing_template_values",
                "Missing required template values: " + ", ".join(sorted(missing)),
            )
        elif len(rendered) > SMS_MAX_BODY_CHARACTERS:
            eligibility = (
                "message_too_long",
                f"Rendered SMS exceeds {SMS_MAX_BODY_CHARACTERS} characters",
            )
        try:
            phone = normalize_e164_phone(lead.phone)
        except ValueError:
            phone = ""
        prepared.append((ordinal, requested_id, lead, phone, rendered))
        if eligibility:
            code, reason = eligibility
            message = SmsMessage(
                tenant_id=context.tenant.id,
                batch_id=batch.id,
                lead_id=lead.id,
                message_template_id=template.id,
                template_updated_at=template.updated_at,
                template_name_snapshot=template.name,
                template_body_snapshot=template.body,
                rendered_body=rendered,
                recipient_phone=phone or None,
                sender_snapshot=provider.from_number,
                contact_type=payload.contact_type.value,
                requested_by_user_id=context.user.id,
                status="skipped",
                provider_error_code=code,
                failure_reason=reason,
            )
            database_session.add(message)
            database_session.flush()
            add_message_event(
                database_session,
                message,
                "skipped",
                actor_user_id=context.user.id,
                provider_error_code=code,
                safe_reason=reason,
            )
            database_session.add(
                SmsBatchRecipient(
                    tenant_id=context.tenant.id,
                    batch_id=batch.id,
                    requested_lead_id=requested_id,
                    lead_id=lead.id,
                    sms_message_id=message.id,
                    ordinal=ordinal,
                    status="skipped",
                    reason_code=code,
                    reason=reason,
                    warnings=[],
                )
            )
            completed_recipient_ids.add(requested_id)

    valid_phones = Counter(
        phone for _, _, lead, phone, _ in prepared
        if phone and _eligibility(lead) is None
    )
    for ordinal, requested_id, lead, phone, rendered in prepared:
        if requested_id in completed_recipient_ids:
            continue
        status = "unknown"
        code = reason = None
        if valid_phones[phone] > 1:
            status = "skipped"
            code = "duplicate_recipient"
            reason = "Multiple selected leads have the same SMS recipient"
        message = SmsMessage(
            tenant_id=context.tenant.id,
            batch_id=batch.id,
            lead_id=lead.id,
            message_template_id=template.id,
            template_updated_at=template.updated_at,
            template_name_snapshot=template.name,
            template_body_snapshot=template.body,
            rendered_body=rendered,
            recipient_phone=phone,
            sender_snapshot=provider.from_number,
            contact_type=payload.contact_type.value,
            requested_by_user_id=context.user.id,
            status=status,
            provider_error_code=code,
            failure_reason=reason,
        )
        database_session.add(message)
        database_session.flush()
        if status == "skipped":
            add_message_event(
                database_session,
                message,
                status,
                actor_user_id=context.user.id,
                provider_error_code=code,
                safe_reason=reason,
            )
        database_session.add(
            SmsBatchRecipient(
                tenant_id=context.tenant.id,
                batch_id=batch.id,
                requested_lead_id=requested_id,
                lead_id=lead.id,
                sms_message_id=message.id,
                ordinal=ordinal,
                status=status,
                reason_code=code,
                reason=reason,
                warnings=[],
            )
        )

    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="sms.batch.created",
        target_membership_id=context.membership.id,
        details={
            "sms_batch_id": str(batch.id),
            "selection_count": batch.selection_count,
            "contact_type": batch.contact_type,
        },
    )
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        existing = _existing_batch(database_session, context, payload)
        if existing is not None:
            return existing, True
        raise exception
    database_session.refresh(batch)
    return batch, False


async def _provider_send(
    provider: TwilioSmsProvider, message: SmsMessage
) -> tuple[str, TwilioSmsResult | TwilioSmsError | None]:
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                provider.send_message,
                to=message.recipient_phone,
                body=message.rendered_body,
            ),
            timeout=settings.sms_direct_send_timeout_seconds,
        )
        return "result", result
    except TwilioSmsRejectedError as exception:
        return "rejected", exception
    except TwilioSmsConfigurationError as exception:
        return "configuration", exception
    except TwilioSmsPreflightError as exception:
        return "preflight", exception
    except TwilioSmsProviderFailureError as exception:
        return "provider_failure", exception
    except TwilioSmsAcceptanceUnknownError as exception:
        return "unknown", exception
    except asyncio.TimeoutError:
        return "unknown", None
    except Exception:
        return "provider_failure", None


def _outcome_already_persisted(
    message: SmsMessage,
    kind: str,
    result: TwilioSmsResult | TwilioSmsError | None,
) -> bool:
    if kind == "result" and isinstance(result, TwilioSmsResult):
        return (
            message.provider_message_sid == result.sid
            and message.status == result.status
        )
    if kind in {"rejected", "configuration", "preflight", "provider_failure"}:
        expected_code = result.code if isinstance(result, TwilioSmsError) else "provider_failure"
        return (
            message.status == "failed"
            and message.provider_error_code == expected_code
            and message.failure_reason is not None
        )
    return (
        message.status == "unknown"
        and message.provider_error_code == "acceptance_unknown"
        and message.failure_reason is not None
    )


def _apply_provider_outcome(
    database_session: Session,
    context: WorkspaceContext,
    message: SmsMessage,
    kind: str,
    result: TwilioSmsResult | TwilioSmsError | None,
) -> None:
    now = _now()
    if kind == "result" and isinstance(result, TwilioSmsResult):
        message.provider_message_sid = result.sid
        message.provider_error_code = result.error_code
        message.failure_reason = None
        _mark_state(message, result.status, now)
        if result.status in {"failed", "undelivered"}:
            message.failure_reason = safe_twilio_rejection_message(result.error_code)
    elif kind in {"rejected", "configuration", "preflight", "provider_failure"}:
        message.provider_error_code = (
            result.code if isinstance(result, TwilioSmsError) else "provider_failure"
        )
        message.failure_reason = (
            result.safe_message
            if isinstance(result, TwilioSmsError)
            else "SMS provider processing failed before acceptance was confirmed"
        )
        _mark_state(message, "failed", now)
    else:
        message.provider_error_code = "acceptance_unknown"
        message.failure_reason = (
            result.safe_message
            if isinstance(result, TwilioSmsError)
            else "Twilio SMS acceptance could not be determined"
        )
        _mark_state(message, "unknown", now)
    add_message_event(
        database_session,
        message,
        message.status,
        actor_user_id=context.user.id,
        provider_error_code=message.provider_error_code,
        safe_reason=message.failure_reason,
        occurred_at=now,
    )
    _sync_recipient(database_session, message)
    _add_activity(database_session, context, message, now)


def _persist_provider_outcome(
    database_session: Session,
    context: WorkspaceContext,
    message_id: uuid.UUID,
    kind: str,
    result: TwilioSmsResult | TwilioSmsError | None,
) -> None:
    """Persist a provider result without ever repeating the provider request.

    A second database attempt handles a transient/ambiguous commit failure. Before
    retrying it re-reads the row, because the first COMMIT may have succeeded even
    when the application did not receive its acknowledgement.
    """
    last_error: SQLAlchemyError | None = None
    for _ in range(2):
        try:
            message = get_sms_message(
                database_session,
                context.tenant.id,
                message_id,
                for_update=True,
            )
            if message is None:
                return
            if _outcome_already_persisted(message, kind, result):
                database_session.rollback()
                return
            _apply_provider_outcome(
                database_session, context, message, kind, result
            )
            database_session.commit()
            return
        except SQLAlchemyError as exception:
            last_error = exception
            database_session.rollback()
            database_session.expire_all()
    if last_error is not None:
        raise last_error


async def deliver_sms_batch(
    database_session: Session,
    context: WorkspaceContext,
    batch: SmsBatch,
    provider: TwilioSmsProvider,
) -> SmsBatch:
    messages = [
        message
        for message in list_batch_messages(database_session, batch.id)
        if message.status == "unknown" and message.attempted_at is None
    ]
    sendable: list[SmsMessage] = []
    now = _now()
    for message in messages:
        lead = get_lead(
            database_session,
            context.tenant.id,
            message.lead_id,
            include_archived=True,
        )
        eligibility = (
            ("lead_unavailable", "Lead is unavailable or outside the authorized scope")
            if lead is None or not _scope_allows(context, lead)
            else _eligibility(lead, message.recipient_phone)
        )
        if eligibility:
            message.status = "skipped"
            message.provider_error_code, message.failure_reason = eligibility
            add_message_event(
                database_session,
                message,
                "skipped",
                actor_user_id=context.user.id,
                provider_error_code=message.provider_error_code,
                safe_reason=message.failure_reason,
                occurred_at=now,
            )
            _sync_recipient(database_session, message)
            continue
        message.attempted_at = now
        sendable.append(message)
    database_session.commit()

    semaphore = asyncio.Semaphore(settings.sms_direct_send_concurrency)

    async def bounded(message: SmsMessage):
        async with semaphore:
            return message.id, await _provider_send(provider, message)

    results = await asyncio.gather(*(bounded(message) for message in sendable))
    for message_id, (kind, result) in results:
        _persist_provider_outcome(
            database_session, context, message_id, kind, result
        )
    batch = get_sms_batch(
        database_session, context.tenant.id, batch.id
    )
    if batch is None:
        raise SmsBatchNotFoundError
    _finish_batch(database_session, batch)
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="sms.batch.completed",
        target_membership_id=context.membership.id,
        details={"sms_batch_id": str(batch.id)},
    )
    try:
        database_session.commit()
    except SQLAlchemyError:
        database_session.rollback()
        batch = get_sms_batch(database_session, context.tenant.id, batch.id)
        if batch is None:
            raise SmsBatchNotFoundError
        if batch.status == "processing":
            _finish_batch(database_session, batch)
            database_session.commit()
    database_session.refresh(batch)
    return batch


async def send_sms_batch(
    database_session: Session,
    context: WorkspaceContext,
    payload: SmsSendRequest,
    provider: TwilioSmsProvider,
) -> tuple[SmsBatch, bool]:
    batch, replayed = prepare_sms_batch(
        database_session, context, payload, provider
    )
    if replayed or batch.status != "processing":
        get_sms_batch_record(database_session, context, batch.id)
        return batch, replayed
    return await deliver_sms_batch(
        database_session, context, batch, provider
    ), False


def get_sms_batch_record(
    database_session: Session,
    context: WorkspaceContext,
    batch_id: uuid.UUID,
) -> SmsBatch:
    batch = get_sms_batch(database_session, context.tenant.id, batch_id)
    if batch is None:
        raise SmsBatchNotFoundError
    if context.role == RoleKey.STAFF.value:
        unauthorized_message = database_session.scalar(
            select(SmsMessage.id)
            .join(Lead, Lead.id == SmsMessage.lead_id)
            .where(
                SmsMessage.batch_id == batch.id,
                or_(
                    Lead.assigned_user_id.is_(None),
                    Lead.assigned_user_id != context.user.id,
                ),
            )
            .limit(1)
        )
        if batch.created_by_user_id != context.user.id or unauthorized_message is not None:
            raise SmsBatchNotFoundError
    return batch


def recover_sms_batch_by_idempotency(
    database_session: Session,
    context: WorkspaceContext,
    idempotency_key: str,
) -> SmsBatch | None:
    batch = get_batch_by_idempotency(
        database_session, context.tenant.id, idempotency_key
    )
    # Recovery is deliberately limited to the actor who originated the operation.
    # Returning the same missing result prevents idempotency-key probing.
    if batch is None or batch.created_by_user_id != context.user.id:
        return None
    return get_sms_batch_record(database_session, context, batch.id)


def get_sms_message_record(
    database_session: Session,
    context: WorkspaceContext,
    message_id: uuid.UUID,
) -> SmsMessage:
    message = get_sms_message(database_session, context.tenant.id, message_id)
    if message is None:
        raise SmsMessageNotFoundError
    lead = get_lead(
        database_session,
        context.tenant.id,
        message.lead_id,
        include_archived=True,
    )
    if lead is None or not _scope_allows(context, lead):
        raise SmsMessageNotFoundError
    return message


async def refresh_sms_status(
    database_session: Session,
    context: WorkspaceContext,
    message_id: uuid.UUID,
    provider: TwilioSmsProvider,
) -> SmsRefreshResult:
    message = get_sms_message_record(database_session, context, message_id)
    if not message.provider_message_sid:
        raise SmsStatusRefreshError("This SMS has no provider message ID to refresh")
    if message.status in {"delivered", "failed", "undelivered"}:
        return SmsRefreshResult(message, False, None)
    now = _now()
    if message.last_status_checked_at is not None:
        checked = message.last_status_checked_at
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        next_refresh = checked + timedelta(seconds=settings.sms_status_refresh_min_seconds)
        if next_refresh > now:
            return SmsRefreshResult(message, False, next_refresh)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(provider.fetch_message, message.provider_message_sid),
            timeout=settings.sms_direct_send_timeout_seconds,
        )
    except (TwilioSmsError, asyncio.TimeoutError) as exception:
        raise SmsStatusRefreshError(
            exception.safe_message
            if isinstance(exception, TwilioSmsError)
            else "Twilio message status lookup timed out"
        ) from exception
    message = get_sms_message(
        database_session, context.tenant.id, message.id, for_update=True
    )
    if message is None:
        raise SmsMessageNotFoundError
    message.last_status_checked_at = now
    changed = message.status != result.status
    message.provider_error_code = result.error_code
    if changed:
        _mark_state(message, result.status, now)
        if result.status in {"failed", "undelivered"}:
            message.failure_reason = safe_twilio_rejection_message(result.error_code)
        add_message_event(
            database_session,
            message,
            message.status,
            actor_user_id=context.user.id,
            provider_error_code=message.provider_error_code,
            safe_reason=message.failure_reason,
            occurred_at=now,
        )
        _sync_recipient(database_session, message)
    database_session.commit()
    database_session.refresh(message)
    return SmsRefreshResult(
        message,
        True,
        now + timedelta(seconds=settings.sms_status_refresh_min_seconds),
    )


def sms_batch_recipients(database_session: Session, batch: SmsBatch):
    return list_batch_recipients(database_session, batch.id)


def sms_message_events(database_session: Session, message: SmsMessage):
    return list_message_events(database_session, message.id)
