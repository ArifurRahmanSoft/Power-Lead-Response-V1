import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.email_consent import email_consent_denial_message
from app.core.config import settings
from app.core.lead_catalogs import LeadStatus
from app.core.permissions import RoleKey
from app.core.security import WorkspaceContext
from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead
from app.models.lead_activity import LeadActivity
from app.models.outbound_message import OutboundMessage, OutboundMessageJob, OutboundMessagePreview
from app.repositories.customer_repository import get_customer, list_customer_conversions
from app.repositories.outbound_message_repository import (
    OutboundMessagePage,
    add_event,
    get_by_idempotency,
    get_job_for_message,
    get_message,
    get_preview,
    list_events,
    list_messages,
)
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.outbound_message import OutboundPreviewRequest, OutboundSendRequest
from app.services.lead_service import get_lead_for_context
from app.services.mail_template_service import get_template_record
from app.services.sender_account_service import get_sender_for_context
from app.utils.lead_normalization import normalize_country_name
from app.utils.mail_template import (
    PLACEHOLDER_FIELDS,
    extract_asset_ids,
    html_to_plain_text,
    render_text_template,
)


class OutboundMessageNotFoundError(Exception): pass
class OutboundMessagePermissionError(Exception): pass
class OutboundMessageEligibilityError(Exception): pass
class OutboundPreviewStaleError(Exception): pass
class OutboundPreviewExpiredError(Exception): pass
class OutboundIdempotencyConflictError(Exception): pass
class OutboundMessageStateError(Exception): pass


@dataclass(frozen=True)
class ResolvedContext:
    lead: Lead
    customer: Customer | None
    recipient: str
    values: dict[str, object]


def _staff_scope(context: WorkspaceContext) -> uuid.UUID | None:
    return context.user.id if context.role == RoleKey.STAFF.value else None


def _lead_values(lead: Lead) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in PLACEHOLDER_FIELDS:
        value = getattr(lead, field, None)
        if field == "country" and value:
            value = normalize_country_name(value)
        values[field] = value
    return values


def _resolve_context(session: Session, context: WorkspaceContext, payload: OutboundPreviewRequest) -> ResolvedContext:
    if payload.context_type.value == "lead":
        lead = get_lead_for_context(session, context, payload.context_id)
        return ResolvedContext(lead, None, lead.email.lower(), _lead_values(lead))
    customer = get_customer(
        session,
        tenant_id=context.tenant.id,
        customer_id=payload.context_id,
        assigned_scope_user_id=_staff_scope(context),
    )
    if customer is None:
        raise OutboundMessageNotFoundError
    conversions = list_customer_conversions(
        session,
        tenant_id=context.tenant.id,
        customer_id=customer.id,
        assigned_scope_user_id=_staff_scope(context),
    )
    if not conversions:
        raise OutboundMessageEligibilityError("Customer has no authorized source lead")
    lead = get_lead_for_context(session, context, conversions[0].lead_id)
    values = _lead_values(lead)
    values.update({
        "first_name": customer.first_name,
        "last_name": customer.last_name,
        "email": customer.email,
        "phone": customer.phone,
        "company": customer.company,
    })
    return ResolvedContext(lead, customer, customer.email.lower(), values)


def _check_eligibility(resolved: ResolvedContext, purpose: str) -> None:
    if purpose == "lead_acquisition":
        if resolved.customer is not None or resolved.lead.status == LeadStatus.CONVERTED.value:
            raise OutboundMessageEligibilityError("Acquisition messages are not allowed for converted customers")
        if resolved.lead.status == LeadStatus.DISQUALIFIED.value:
            raise OutboundMessageEligibilityError("Closed or disqualified leads cannot receive acquisition messages")
        if resolved.lead.deleted_at is not None:
            raise OutboundMessageEligibilityError("Archived leads cannot receive messages")
        consent_denial = email_consent_denial_message(
            "Lead", resolved.lead.consent_status
        )
        if consent_denial:
            raise OutboundMessageEligibilityError(consent_denial)
        if resolved.lead.email_status in {"invalid", "bounced", "disposable"}:
            raise OutboundMessageEligibilityError("Lead email is not eligible for sending")
        return
    customer = resolved.customer
    if customer is None:
        raise OutboundMessageEligibilityError("Customer service messages require a customer context")
    consent_denial = email_consent_denial_message(
        "Customer", customer.communication_consent_status
    )
    if consent_denial:
        raise OutboundMessageEligibilityError(consent_denial)
    if customer.suppressed_at is not None or "email" in {
        str(value).casefold() for value in (customer.channel_restrictions or [])
    }:
        raise OutboundMessageEligibilityError("Customer email communication is suppressed")


def _render_snapshot(session: Session, context: WorkspaceContext, payload: OutboundPreviewRequest) -> tuple[ResolvedContext, dict[str, object]]:
    resolved = _resolve_context(session, context, payload)
    _check_eligibility(resolved, payload.purpose.value)
    sender = get_sender_for_context(session, context, payload.sender_account_id)
    if not sender.is_active or not sender.credential_ciphertext:
        raise OutboundMessageEligibilityError("Sender account is not active and configured")
    template, version, assets = get_template_record(
        session, context, payload.template_id, version_number=payload.template_version
    )
    if template.archived_at is not None or not template.enabled or version.approval_status != "approved":
        raise OutboundMessageEligibilityError("An enabled approved template version is required")
    subject, subject_missing = render_text_template(version.subject, resolved.values, escape_html=False)
    body, body_missing = render_text_template(version.body_html, resolved.values, escape_html=True)
    signature, signature_missing = render_text_template(version.signature_html, resolved.values, escape_html=True)
    missing = subject_missing | body_missing | signature_missing
    if missing:
        raise OutboundMessageEligibilityError("Missing required template values: " + ", ".join(sorted(missing)))
    if "\r" in subject or "\n" in subject:
        raise OutboundMessageEligibilityError("Rendered subject contains an unsafe header value")
    html = body + (f'<div class="mail-signature">{signature}</div>' if signature else "")
    referenced_assets = extract_asset_ids(html)
    available_assets = {asset.id for asset in assets}
    if not referenced_assets <= available_assets:
        raise OutboundMessageEligibilityError("Template references unavailable inline images")
    included_assets = [
        asset for asset in assets
        if asset.kind == "attachment" or asset.id in referenced_assets
    ]
    manifest = [{
        "id": str(asset.id),
        "kind": asset.kind,
        "storage_key": asset.storage_key,
        "filename": asset.original_filename,
        "content_type": asset.content_type,
        "size_bytes": asset.size_bytes,
        "sha256": asset.sha256,
    } for asset in included_assets]
    sender_snapshot = {
        "display_name": sender.display_name,
        "sender_email": sender.sender_email,
        "reply_to": sender.reply_to,
        "configuration_version": sender.updated_at.isoformat() if sender.updated_at else None,
    }
    snapshot: dict[str, object] = {
        "context_type": payload.context_type.value,
        "lead_id": resolved.lead.id,
        "customer_id": resolved.customer.id if resolved.customer else None,
        "sender_account_id": sender.id,
        "template_id": template.id,
        "template_version_id": version.id,
        "template_version": version.version,
        "purpose": payload.purpose.value,
        "recipient_email": resolved.recipient,
        "sender_snapshot": sender_snapshot,
        "subject": subject,
        "body_html": html,
        "body_text": html_to_plain_text(html),
        "asset_manifest": manifest,
    }
    snapshot["content_hash"] = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return resolved, snapshot


def create_outbound_preview(session: Session, context: WorkspaceContext, payload: OutboundPreviewRequest) -> OutboundMessagePreview:
    _, snapshot = _render_snapshot(session, context, payload)
    now = datetime.now(timezone.utc)
    preview = OutboundMessagePreview(
        tenant_id=context.tenant.id,
        created_by=context.user.id,
        expires_at=now + timedelta(minutes=settings.mail_preview_expire_minutes),
        **snapshot,
    )
    session.add(preview)
    session.commit()
    session.refresh(preview)
    return preview


def _payload_for_preview(preview: OutboundMessagePreview) -> OutboundPreviewRequest:
    return OutboundPreviewRequest(
        context_type=preview.context_type,
        context_id=preview.lead_id if preview.context_type == "lead" else preview.customer_id,
        sender_account_id=preview.sender_account_id,
        template_id=preview.template_id,
        template_version=preview.template_version,
        purpose=preview.purpose,
    )


def enqueue_outbound_message(session: Session, context: WorkspaceContext, payload: OutboundSendRequest) -> tuple[OutboundMessage, OutboundMessageJob, bool]:
    request_hash = hashlib.sha256(f"{payload.preview_id}:{payload.preview_hash}".encode()).hexdigest()
    existing = get_by_idempotency(session, context.tenant.id, payload.idempotency_key)
    if existing:
        if existing.request_hash != request_hash:
            raise OutboundIdempotencyConflictError
        job = get_job_for_message(session, existing.id)
        if job is None:
            raise OutboundMessageStateError
        return existing, job, True
    preview = get_preview(session, context.tenant.id, payload.preview_id, for_update=True)
    now = datetime.now(timezone.utc)
    if preview is None or preview.created_by != context.user.id:
        raise OutboundMessageNotFoundError
    preview_expiry = preview.expires_at.replace(tzinfo=timezone.utc) if preview.expires_at.tzinfo is None else preview.expires_at
    if preview_expiry <= now:
        raise OutboundPreviewExpiredError
    if preview.content_hash != payload.preview_hash:
        raise OutboundPreviewStaleError
    _, current = _render_snapshot(session, context, _payload_for_preview(preview))
    if current["content_hash"] != preview.content_hash:
        raise OutboundPreviewStaleError
    message = OutboundMessage(
        tenant_id=context.tenant.id,
        preview_id=preview.id,
        requested_by=context.user.id,
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        **current,
    )
    session.add(message)
    session.flush()
    job = OutboundMessageJob(tenant_id=context.tenant.id, message_id=message.id, available_at=now)
    session.add(job)
    session.flush()
    activity_type = "follow_up" if session.scalar(select(func.count(LeadActivity.id)).where(
        LeadActivity.tenant_id == context.tenant.id,
        LeadActivity.lead_id == message.lead_id,
        LeadActivity.direction == "outbound",
    )) else "initial_contact"
    session.add(LeadActivity(
        tenant_id=context.tenant.id,
        lead_id=message.lead_id,
        activity_type=activity_type,
        channel="email",
        direction="outbound",
        occurred_at=now,
        performed_by=context.user.id,
        outcome="queued",
        notes="System email queued; see message state history.",
        recorded_by=context.user.id,
        is_system=True,
        externally_performed=False,
        provider_verified=False,
        outbound_message_id=message.id,
        contact_stage="initial" if activity_type == "initial_contact" else "follow_up",
    ))
    add_event(session, message, "queued", actor_user_id=context.user.id, occurred_at=now)
    preview.consumed_at = now
    add_authorization_audit(session, tenant_id=context.tenant.id, actor_user_id=context.user.id,
        action="mail.message.queued", target_membership_id=context.membership.id,
        details={"message_id": str(message.id), "lead_id": str(message.lead_id), "purpose": message.purpose})
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = get_by_idempotency(session, context.tenant.id, payload.idempotency_key)
        if existing and existing.request_hash == request_hash:
            job = get_job_for_message(session, existing.id)
            if job:
                return existing, job, True
        raise OutboundIdempotencyConflictError
    return message, job, False


def get_outbound_message(
    session: Session,
    context: WorkspaceContext,
    message_id: uuid.UUID,
) -> tuple[OutboundMessage, OutboundMessageJob | None]:
    message = get_message(session, context.tenant.id, message_id)
    if message is None:
        raise OutboundMessageNotFoundError
    if context.role == RoleKey.STAFF.value:
        lead = get_lead_for_context(session, context, message.lead_id)
        if lead.assigned_user_id != context.user.id:
            raise OutboundMessagePermissionError
    job = get_job_for_message(session, message.id)
    message.message_events = list_events(session, message.id)
    return message, job


def get_outbound_messages(session: Session, context: WorkspaceContext, *, page: int, size: int, lead_id: uuid.UUID | None, customer_id: uuid.UUID | None, status: str | None) -> OutboundMessagePage:
    if lead_id is not None and customer_id is not None:
        raise OutboundMessageStateError(
            "Use either lead_id or customer_id, not both"
        )
    if lead_id is not None:
        # Resolve through the existing tenant and staff-assignment visibility rules
        # before querying history. A foreign ID must not be treated as a valid empty
        # history response.
        get_lead_for_context(session, context, lead_id)
    if customer_id is not None:
        customer = get_customer(
            session,
            tenant_id=context.tenant.id,
            customer_id=customer_id,
            assigned_scope_user_id=_staff_scope(context),
        )
        if customer is None:
            raise OutboundMessageNotFoundError
    result = list_messages(session, tenant_id=context.tenant.id, page=page, size=size,
        lead_id=lead_id, customer_id=customer_id, status=status,
        assigned_scope_user_id=_staff_scope(context))
    for item in result.items:
        item.message_events = list_events(session, item.id)
        item.outbox_job = get_job_for_message(session, item.id)
    return result


def cancel_outbound_message(session: Session, context: WorkspaceContext, message_id: uuid.UUID, reason: str) -> tuple[OutboundMessage, OutboundMessageJob]:
    message = get_message(session, context.tenant.id, message_id, for_update=True)
    if message is None:
        raise OutboundMessageNotFoundError
    if context.role == RoleKey.STAFF.value:
        lead = get_lead_for_context(session, context, message.lead_id)
        if lead.assigned_user_id != context.user.id:
            raise OutboundMessagePermissionError
    job = get_job_for_message(session, message.id, for_update=True)
    if job is None:
        raise OutboundMessageStateError
    if message.status == "processing":
        raise OutboundMessageStateError("Message is already being sent and cannot be safely cancelled")
    if message.status not in {"queued"} or job.status not in {"queued", "retry"}:
        raise OutboundMessageStateError("Only unsent queued messages can be cancelled")
    now = datetime.now(timezone.utc)
    message.status = "cancelled"
    message.cancelled_at = now
    message.cancelled_by = context.user.id
    message.cancellation_reason = reason
    job.status = "cancelled"
    add_event(session, message, "cancelled", actor_user_id=context.user.id, safe_reason=reason, occurred_at=now)
    add_authorization_audit(session, tenant_id=context.tenant.id, actor_user_id=context.user.id,
        action="mail.message.cancelled", target_membership_id=context.membership.id,
        details={"message_id": str(message.id)})
    session.commit()
    message.message_events = list_events(session, message.id)
    return message, job
