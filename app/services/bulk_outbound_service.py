import asyncio
import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import RoleKey
from app.core.security import WorkspaceContext
from app.core.storage import ObjectStorage, ObjectStorageError
from app.models.lead import Lead
from app.models.lead_activity import LeadActivity
from app.models.outbound_message import (
    OutboundBatchRecipient,
    OutboundMessage,
    OutboundMessageBatch,
    OutboundMessageJob,
    OutboundMessagePreview,
)
from app.models.sender_account import SenderAccount
from app.providers.smtp import (
    SmtpAmbiguousDeliveryError,
    SmtpAuthenticationError,
    SmtpConnectionError,
    SmtpProvider,
    SmtpProviderError,
    SmtpSecurityError,
)
from app.repositories.lead_repository import get_lead, list_authorized_lead_ids
from app.repositories.outbound_message_repository import (
    BatchRecipientPage,
    active_message_for_lead,
    add_event,
    all_batch_recipients,
    get_batch,
    get_batch_by_idempotency,
    get_job_for_message,
    get_preview,
    list_batch_recipients,
    list_events,
    recent_active_count,
    recent_sent_count,
)
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.outbound_message import (
    BulkConfirmRequest,
    BulkPreviewRequest,
    DirectBatchSendRequest,
    OutboundPreviewRequest,
)
from app.services.lead_service import LeadNotFoundError, LeadScopeError, get_lead_for_context
from app.services.mail_template_service import get_template_record
from app.services.mail_composition_service import (
    MailCompositionError,
    compose_message_snapshot,
)
from app.services.outbound_message_service import (
    OutboundIdempotencyConflictError,
    OutboundMessageEligibilityError,
    OutboundMessageNotFoundError,
    OutboundMessageStateError,
    OutboundPreviewExpiredError,
    OutboundPreviewStaleError,
    _render_snapshot,
)
from app.services.outbound_worker_service import message_eligibility_error
from app.services.sender_account_service import (
    get_sender_for_context,
    sender_delivery_config,
)
from app.utils.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryptionNotConfiguredError,
)


class BulkSelectionLimitError(Exception):
    def __init__(self, count: int, maximum: int) -> None:
        self.count = count
        self.maximum = maximum


class BulkBatchNotFoundError(Exception): pass


def _assigned_scope(context: WorkspaceContext) -> uuid.UUID | None:
    return context.user.id if context.role == RoleKey.STAFF.value else None


def _selection(
    session: Session,
    context: WorkspaceContext,
    payload: BulkPreviewRequest,
    *,
    limit: int,
) -> tuple[list[tuple[uuid.UUID, Lead | None]], dict[str, object]]:
    if payload.selection_mode == "selected":
        unique_ids = list(dict.fromkeys(payload.lead_ids or []))
        if len(unique_ids) > limit:
            raise BulkSelectionLimitError(len(unique_ids), limit)
        selected: list[tuple[uuid.UUID, Lead | None]] = []
        for lead_id in unique_ids:
            lead = get_lead(session, context.tenant.id, lead_id)
            if lead is not None and (_assigned_scope(context) is None or lead.assigned_user_id == context.user.id):
                selected.append((lead_id, lead))
            else:
                selected.append((lead_id, None))
        return selected, {"mode": "selected", "lead_ids": [str(value) for value in unique_ids]}
    assert payload.filters is not None
    ids, total = list_authorized_lead_ids(
        session,
        tenant_id=context.tenant.id,
        filters=payload.filters,
        assigned_scope_user_id=_assigned_scope(context),
        limit=limit + 1,
    )
    if total > limit:
        raise BulkSelectionLimitError(total, limit)
    leads = [(lead_id, lead) for lead_id in ids if (lead := get_lead(session, context.tenant.id, lead_id)) is not None]
    return leads, {"mode": "all_matching", "filters": payload.filters.model_dump(mode="json"), "lead_ids": [str(value) for value in ids]}


def _stage_warnings(session: Session, lead: Lead, stage: str) -> list[str]:
    sent_stages = set(session.scalars(select(OutboundMessage.contact_stage).where(
        OutboundMessage.tenant_id == lead.tenant_id,
        OutboundMessage.lead_id == lead.id,
        OutboundMessage.status == "sent",
    )).all())
    manual_exists = session.scalar(select(LeadActivity.id).where(
        LeadActivity.tenant_id == lead.tenant_id,
        LeadActivity.lead_id == lead.id,
        LeadActivity.channel == "email",
        LeadActivity.direction == "outbound",
        LeadActivity.is_system.is_(False),
    ).limit(1)) is not None
    has_contact = bool(sent_stages) or manual_exists
    if stage == "initial" and has_contact:
        return ["Initial contact selected, but prior successful or manually recorded outbound email exists."]
    required_prior = {
        "follow_up": (bool(sent_stages) or manual_exists, "initial contact"),
        "second_follow_up": ("follow_up" in sent_stages, "successful first follow-up"),
        "final_follow_up": ("second_follow_up" in sent_stages, "successful second follow-up"),
    }
    if stage in required_prior and not required_prior[stage][0]:
        return [f"{stage.replace('_', ' ').title()} selected, but no {required_prior[stage][1]} exists."]
    return []


def _supersede_untouched_queued_message(
    session: Session,
    context: WorkspaceContext,
    *,
    lead_id: uuid.UUID,
    expected_message_id: uuid.UUID,
    replacement_batch_id: uuid.UUID,
) -> bool:
    """Cancel a worker message only before it can be claimed or attempted.

    The job row is locked first, matching the worker's claim order. A worker
    that won the race leaves a lease or a processing state and cannot be
    superseded by this path.
    """
    job = session.scalar(
        select(OutboundMessageJob)
        .where(OutboundMessageJob.message_id == expected_message_id)
        .with_for_update()
    )
    if job is None:
        return False
    message = session.scalar(
        select(OutboundMessage)
        .where(
            OutboundMessage.id == expected_message_id,
            OutboundMessage.tenant_id == context.tenant.id,
            OutboundMessage.lead_id == lead_id,
        )
        .with_for_update()
    )
    if (
        message is None
        or message.status != "queued"
        or job.status not in {"queued", "retry"}
        or job.lease_token is not None
        or job.lease_expires_at is not None
        or message.provider_attempt_started_at is not None
    ):
        return False

    now = datetime.now(timezone.utc)
    reason = (
        "Superseded by an explicitly requested direct-send batch "
        f"{replacement_batch_id}"
    )
    message.status = "cancelled"
    message.cancelled_at = now
    message.cancelled_by = context.user.id
    message.cancellation_reason = reason
    job.status = "cancelled"
    job.last_error_code = "superseded_by_direct_send"
    add_event(
        session,
        message,
        "cancelled",
        actor_user_id=context.user.id,
        safe_reason=reason,
        occurred_at=now,
    )
    add_authorization_audit(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="mail.message.superseded_by_direct_send",
        target_membership_id=context.membership.id,
        details={
            "message_id": str(message.id),
            "job_id": str(job.id),
            "replacement_batch_id": str(replacement_batch_id),
        },
    )
    return True


def create_bulk_preview(
    session: Session,
    context: WorkspaceContext,
    payload: BulkPreviewRequest,
    *,
    commit: bool = True,
    audit_action: str = "mail.batch.previewed",
    selection_limit: int | None = None,
    supersede_queued: bool = False,
) -> OutboundMessageBatch:
    sender = get_sender_for_context(session, context, payload.sender_account_id)
    if not sender.is_active or not sender.credential_ciphertext:
        raise OutboundMessageEligibilityError("Sender account is not active and configured")
    template, version, _ = get_template_record(
        session, context, payload.template_id, version_number=payload.template_version
    )
    if template.archived_at is not None or not template.enabled or version.approval_status != "approved":
        raise OutboundMessageEligibilityError("An enabled approved template version is required")
    selected, selection_snapshot = _selection(
        session,
        context,
        payload,
        limit=selection_limit or settings.mail_bulk_max_selection,
    )
    now = datetime.now(timezone.utc)
    batch = OutboundMessageBatch(
        tenant_id=context.tenant.id,
        created_by=context.user.id,
        sender_account_id=sender.id,
        template_id=template.id,
        template_version_id=version.id,
        template_version=version.version,
        contact_stage=payload.contact_stage.value,
        selection_mode=payload.selection_mode,
        selection_snapshot=selection_snapshot,
        preview_hash="0" * 64,
        selection_count=len(selected),
        eligible_count=0,
        blocked_count=0,
        expires_at=now + timedelta(minutes=settings.mail_preview_expire_minutes),
    )
    session.add(batch)
    session.flush()

    email_groups: dict[str, list[Lead]] = defaultdict(list)
    for _, lead in selected:
        if lead is None:
            continue
        email_groups[lead.email.strip().casefold()].append(lead)
    conflict_ids = {lead.id for group in email_groups.values() if len(group) > 1 for lead in group}
    fingerprint_items: list[dict[str, object]] = []
    eligible = blocked = 0
    for ordinal, (requested_lead_id, lead) in enumerate(selected, start=1):
        recipient = OutboundBatchRecipient(
            tenant_id=context.tenant.id,
            batch_id=batch.id,
            requested_lead_id=requested_lead_id,
            lead_id=lead.id if lead else None,
            ordinal=ordinal,
            recipient_email=lead.email.lower() if lead else None,
            preview_status="blocked",
            warnings=_stage_warnings(session, lead, payload.contact_stage.value) if lead else [],
        )
        session.add(recipient)
        if lead is None:
            recipient.exclusion_code = "not_accessible"
            recipient.exclusion_reason = "Lead was not found or is outside your authorized scope"
            blocked += 1
        elif lead.id in conflict_ids:
            recipient.preview_status = "conflict"
            recipient.exclusion_code = "duplicate_recipient"
            recipient.exclusion_reason = "Another selected lead has the same email address"
            blocked += 1
        else:
            active_message = active_message_for_lead(
                session, context.tenant.id, lead.id
            )
            if active_message is not None and not supersede_queued:
                recipient.exclusion_code = "active_send_conflict"
                recipient.exclusion_reason = (
                    "A queued or processing email already exists for this lead"
                )
                blocked += 1
                fingerprint_items.append({
                    "lead_id": str(requested_lead_id),
                    "status": recipient.preview_status,
                    "reason": recipient.exclusion_code,
                    "preview_id": None,
                })
                continue
            try:
                _, snapshot = _render_snapshot(session, context, OutboundPreviewRequest(
                    context_type="lead",
                    context_id=lead.id,
                    sender_account_id=sender.id,
                    template_id=template.id,
                    template_version=version.version,
                    purpose="lead_acquisition",
                ))
                if active_message is not None:
                    if not _supersede_untouched_queued_message(
                        session,
                        context,
                        lead_id=lead.id,
                        expected_message_id=active_message.id,
                        replacement_batch_id=batch.id,
                    ):
                        recipient.exclusion_code = "active_send_conflict"
                        recipient.exclusion_reason = (
                            "A queued or processing email already exists for this lead"
                        )
                        blocked += 1
                        fingerprint_items.append({
                            "lead_id": str(requested_lead_id),
                            "status": recipient.preview_status,
                            "reason": recipient.exclusion_code,
                            "preview_id": None,
                        })
                        continue
                    recipient.warnings = [
                        *(recipient.warnings or []),
                        "An untouched queued email was cancelled as superseded by this direct send.",
                    ]
                preview = OutboundMessagePreview(
                    tenant_id=context.tenant.id,
                    created_by=context.user.id,
                    expires_at=batch.expires_at,
                    **snapshot,
                )
                session.add(preview)
                session.flush()
                recipient.preview_status = "eligible"
                recipient.preview_id = preview.id
                recipient.exclusion_code = None
                recipient.exclusion_reason = None
                eligible += 1
            except OutboundMessageEligibilityError as exception:
                recipient.exclusion_code = "not_eligible"
                recipient.exclusion_reason = str(exception)
                blocked += 1
        fingerprint_items.append({
            "lead_id": str(requested_lead_id),
            "status": recipient.preview_status,
            "reason": recipient.exclusion_code,
            "preview_id": str(recipient.preview_id) if recipient.preview_id else None,
        })
    batch.eligible_count = eligible
    batch.blocked_count = blocked
    batch.preview_hash = hashlib.sha256(json.dumps({
        "batch_id": str(batch.id),
        "selection": selection_snapshot,
        "sender_id": str(sender.id),
        "sender_version": sender.updated_at.isoformat() if sender.updated_at else None,
        "template_version_id": str(version.id),
        "stage": batch.contact_stage,
        "recipients": fingerprint_items,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    add_authorization_audit(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action=audit_action,
        target_membership_id=context.membership.id,
        details={"batch_id": str(batch.id), "selection_count": len(selected), "eligible_count": eligible},
    )
    if commit:
        session.commit()
        session.refresh(batch)
    else:
        session.flush()
    return batch


def get_bulk_batch(session: Session, context: WorkspaceContext, batch_id: uuid.UUID) -> OutboundMessageBatch:
    batch = get_batch(session, context.tenant.id, batch_id)
    if batch is None:
        raise BulkBatchNotFoundError
    return batch


def get_bulk_recipients(session: Session, context: WorkspaceContext, batch_id: uuid.UUID, *, page: int, size: int) -> BatchRecipientPage:
    get_bulk_batch(session, context, batch_id)
    result = list_batch_recipients(session, batch_id, page=page, size=size)
    for item in result.items:
        item.preview = get_preview(session, context.tenant.id, item.preview_id) if item.preview_id else None
        item.message = session.get(OutboundMessage, item.message_id) if item.message_id else None
        item.message_events = list_events(session, item.message_id) if item.message_id else []
    return result


def confirm_bulk_batch(
    session: Session,
    context: WorkspaceContext,
    batch_id: uuid.UUID,
    payload: BulkConfirmRequest,
    *,
    request_hash_override: str | None = None,
) -> tuple[OutboundMessageBatch, bool]:
    request_hash = request_hash_override or hashlib.sha256(
        f"{batch_id}:{payload.preview_hash}".encode()
    ).hexdigest()
    existing = get_batch_by_idempotency(session, context.tenant.id, payload.idempotency_key)
    if existing:
        if existing.request_hash != request_hash:
            raise OutboundIdempotencyConflictError
        return existing, True
    batch = get_batch(session, context.tenant.id, batch_id, for_update=True)
    if batch is None or batch.created_by != context.user.id:
        raise BulkBatchNotFoundError
    if batch.idempotency_key is not None:
        if batch.idempotency_key == payload.idempotency_key and batch.request_hash == request_hash:
            return batch, True
        raise OutboundIdempotencyConflictError
    now = datetime.now(timezone.utc)
    expiry = batch.expires_at.replace(tzinfo=timezone.utc) if batch.expires_at.tzinfo is None else batch.expires_at
    if expiry <= now:
        raise OutboundPreviewExpiredError
    if batch.preview_hash != payload.preview_hash or batch.status != "previewed":
        raise OutboundPreviewStaleError
    if batch.eligible_count == 0:
        raise OutboundMessageEligibilityError("Batch contains no eligible recipients")
    recipients = all_batch_recipients(session, batch.id, for_update=True)
    for recipient in recipients:
        if recipient.preview_status != "eligible" or recipient.lead_id is None:
            continue
        preview = get_preview(session, context.tenant.id, recipient.preview_id, for_update=True)
        if preview is None or active_message_for_lead(session, context.tenant.id, recipient.lead_id, exclude_batch_id=batch.id):
            raise OutboundPreviewStaleError
        try:
            _, current = _render_snapshot(session, context, OutboundPreviewRequest(
                context_type="lead", context_id=recipient.lead_id,
                sender_account_id=batch.sender_account_id, template_id=batch.template_id,
                template_version=batch.template_version, purpose="lead_acquisition",
            ))
        except (LeadNotFoundError, LeadScopeError, OutboundMessageEligibilityError) as exception:
            raise OutboundPreviewStaleError from exception
        if current["content_hash"] != preview.content_hash:
            raise OutboundPreviewStaleError
    batch.idempotency_key = payload.idempotency_key
    batch.request_hash = request_hash
    batch.status = "queued"
    batch.confirmed_at = now
    for recipient in recipients:
        if recipient.preview_status != "eligible" or recipient.lead_id is None:
            continue
        preview = get_preview(session, context.tenant.id, recipient.preview_id, for_update=True)
        assert preview is not None
        message = OutboundMessage(
            tenant_id=context.tenant.id,
            preview_id=preview.id,
            requested_by=context.user.id,
            batch_id=batch.id,
            contact_stage=batch.contact_stage,
            context_type=preview.context_type,
            lead_id=preview.lead_id,
            customer_id=preview.customer_id,
            sender_account_id=preview.sender_account_id,
            template_id=preview.template_id,
            template_version_id=preview.template_version_id,
            template_version=preview.template_version,
            purpose=preview.purpose,
            recipient_email=preview.recipient_email,
            sender_snapshot=preview.sender_snapshot,
            subject=preview.subject,
            body_html=preview.body_html,
            body_text=preview.body_text,
            asset_manifest=preview.asset_manifest,
            content_hash=preview.content_hash,
            idempotency_key=f"bulk:{batch.id}:{recipient.lead_id}",
            request_hash=request_hash,
        )
        session.add(message)
        session.flush()
        recipient.message_id = message.id
        session.add(OutboundMessageJob(tenant_id=context.tenant.id, message_id=message.id, available_at=now))
        session.add(LeadActivity(
            tenant_id=context.tenant.id,
            lead_id=recipient.lead_id,
            activity_type="initial_contact" if batch.contact_stage == "initial" else "follow_up",
            channel="email", direction="outbound", occurred_at=now,
            performed_by=context.user.id, outcome="queued",
            notes="System email queued from an explicit lead-list batch.",
            recorded_by=context.user.id, is_system=True, externally_performed=False,
            provider_verified=False, outbound_message_id=message.id,
            contact_stage=batch.contact_stage,
        ))
        add_event(session, message, "queued", actor_user_id=context.user.id, occurred_at=now)
        preview.consumed_at = now
    add_authorization_audit(session, tenant_id=context.tenant.id, actor_user_id=context.user.id,
        action="mail.batch.queued", target_membership_id=context.membership.id,
        details={"batch_id": str(batch.id), "queued_count": batch.eligible_count})
    try:
        session.commit()
    except IntegrityError as exception:
        session.rollback()
        existing = get_batch_by_idempotency(session, context.tenant.id, payload.idempotency_key)
        if existing and existing.request_hash == request_hash:
            return existing, True
        raise OutboundPreviewStaleError from exception
    return batch, False


def _direct_request_hash(
    context: WorkspaceContext,
    payload: DirectBatchSendRequest,
) -> str:
    canonical = payload.model_dump(mode="json", exclude={"idempotency_key"})
    canonical["actor_user_id"] = str(context.user.id)
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _activity_type(contact_stage: str) -> str:
    return "initial_contact" if contact_stage == "initial" else "follow_up"


def _record_direct_activity(
    session: Session,
    *,
    lead_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    contact_stage: str,
    outcome: str,
    notes: str,
    message_id: uuid.UUID | None = None,
    provider_verified: bool = False,
) -> None:
    if message_id is not None and session.scalar(
        select(LeadActivity.id).where(
            LeadActivity.outbound_message_id == message_id
        ).limit(1)
    ) is not None:
        return
    now = datetime.now(timezone.utc)
    session.add(LeadActivity(
        tenant_id=tenant_id,
        lead_id=lead_id,
        activity_type=_activity_type(contact_stage),
        channel="email",
        direction="outbound",
        occurred_at=now,
        performed_by=actor_user_id,
        outcome=outcome,
        notes=notes,
        recorded_by=actor_user_id,
        is_system=True,
        externally_performed=False,
        provider_verified=provider_verified,
        outbound_message_id=message_id,
        contact_stage=contact_stage,
    ))


def prepare_bulk_direct(
    session: Session,
    context: WorkspaceContext,
    payload: DirectBatchSendRequest,
) -> tuple[OutboundMessageBatch, bool]:
    request_hash = _direct_request_hash(context, payload)
    existing = get_batch_by_idempotency(
        session, context.tenant.id, payload.idempotency_key
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise OutboundIdempotencyConflictError
        return existing, True

    # Serialize direct preparation for this sender so concurrent requests count
    # existing rate-limit reservations consistently.
    sender = get_sender_for_context(session, context, payload.sender_account_id)
    session.scalar(
        select(SenderAccount).where(
            SenderAccount.id == sender.id,
            SenderAccount.tenant_id == context.tenant.id,
        ).with_for_update()
    )

    preview_payload = BulkPreviewRequest(
        selection_mode=payload.selection_mode,
        lead_ids=payload.lead_ids,
        filters=payload.filters,
        sender_account_id=payload.sender_account_id,
        template_id=payload.template_id,
        template_version=payload.template_version,
        contact_stage=payload.contact_type,
    )
    batch = create_bulk_preview(
        session,
        context,
        preview_payload,
        commit=False,
        audit_action="mail.batch.prepared_for_direct_send",
        selection_limit=settings.mail_direct_send_max_recipients,
        supersede_queued=payload.supersede_queued,
    )
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=1)
    sender_capacity = max(
        0,
        settings.mail_worker_sender_rate_per_minute
        - recent_sent_count(
            session,
            tenant_id=context.tenant.id,
            sender_account_id=sender.id,
            since=since,
        )
        - recent_active_count(
            session,
            tenant_id=context.tenant.id,
            sender_account_id=sender.id,
            since=since,
        ),
    )
    workspace_capacity = max(
        0,
        settings.mail_worker_workspace_rate_per_minute
        - recent_sent_count(session, tenant_id=context.tenant.id, since=since)
        - recent_active_count(session, tenant_id=context.tenant.id, since=since),
    )
    available_capacity = min(sender_capacity, workspace_capacity)
    recipients = all_batch_recipients(session, batch.id, for_update=True)
    eligible_seen = 0
    for recipient in recipients:
        if recipient.preview_status != "eligible" or recipient.lead_id is None:
            if recipient.lead_id is not None:
                _record_direct_activity(
                    session,
                    lead_id=recipient.lead_id,
                    actor_user_id=context.user.id,
                    tenant_id=context.tenant.id,
                    contact_stage=batch.contact_stage,
                    outcome="skipped",
                    notes=recipient.exclusion_reason or "Direct email was skipped.",
                )
            continue
        if eligible_seen >= available_capacity:
            recipient.preview_status = "blocked"
            recipient.exclusion_code = "rate_limit_exceeded"
            recipient.exclusion_reason = (
                "Sender or workspace direct-email rate limit is currently exhausted"
            )
            batch.eligible_count -= 1
            batch.blocked_count += 1
            _record_direct_activity(
                session,
                lead_id=recipient.lead_id,
                actor_user_id=context.user.id,
                tenant_id=context.tenant.id,
                contact_stage=batch.contact_stage,
                outcome="skipped",
                notes=recipient.exclusion_reason,
            )
            continue
        eligible_seen += 1
        preview = get_preview(
            session, context.tenant.id, recipient.preview_id, for_update=True
        )
        assert preview is not None
        message = OutboundMessage(
            tenant_id=context.tenant.id,
            preview_id=preview.id,
            requested_by=context.user.id,
            batch_id=batch.id,
            contact_stage=batch.contact_stage,
            context_type=preview.context_type,
            lead_id=preview.lead_id,
            customer_id=preview.customer_id,
            sender_account_id=preview.sender_account_id,
            template_id=preview.template_id,
            template_version_id=preview.template_version_id,
            template_version=preview.template_version,
            purpose=preview.purpose,
            recipient_email=preview.recipient_email,
            sender_snapshot=preview.sender_snapshot,
            subject=preview.subject,
            body_html=preview.body_html,
            body_text=preview.body_text,
            asset_manifest=preview.asset_manifest,
            content_hash=preview.content_hash,
            idempotency_key=f"direct:{batch.id}:{recipient.lead_id}",
            request_hash=request_hash,
            status="processing",
            processing_at=now,
        )
        session.add(message)
        session.flush()
        recipient.message_id = message.id
        preview.consumed_at = now
        add_event(
            session,
            message,
            "processing",
            actor_user_id=context.user.id,
            occurred_at=now,
        )

    batch.idempotency_key = payload.idempotency_key
    batch.request_hash = request_hash
    batch.status = "processing" if batch.eligible_count else "completed"
    batch.confirmed_at = now
    add_authorization_audit(
        session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action=(
            "mail.batch.direct_send_started"
            if batch.eligible_count
            else "mail.batch.direct_send_no_eligible_recipients"
        ),
        target_membership_id=context.membership.id,
        details={
            "batch_id": str(batch.id),
            "selection_count": batch.selection_count,
            "attempt_count": batch.eligible_count,
            "skipped_count": batch.blocked_count,
        },
    )
    try:
        session.commit()
    except IntegrityError as exception:
        session.rollback()
        existing = get_batch_by_idempotency(
            session, context.tenant.id, payload.idempotency_key
        )
        if existing is not None and existing.request_hash == request_hash:
            return existing, True
        raise OutboundPreviewStaleError from exception
    return batch, False


def _finalize_direct_message(
    session_factory,
    message_id: uuid.UUID,
    status: str,
    *,
    code: str | None = None,
    reason: str | None = None,
) -> None:
    with session_factory() as session:
        message = session.scalar(
            select(OutboundMessage).where(
                OutboundMessage.id == message_id
            ).with_for_update()
        )
        if message is None or message.status in {
            "sent", "failed", "unknown", "cancelled", "skipped"
        }:
            return
        now = datetime.now(timezone.utc)
        message.status = status
        message.failure_code = code
        message.failure_reason = reason
        if status == "sent":
            message.sent_at = now
            notes = "SMTP server accepted the email; delivery and read status are not verified."
        elif status == "skipped":
            notes = reason or "Direct email was skipped before SMTP."
        else:
            message.failed_at = now
            notes = reason or "Direct email processing failed."
        add_event(session, message, status, safe_reason=reason, occurred_at=now)
        assert message.lead_id is not None
        _record_direct_activity(
            session,
            lead_id=message.lead_id,
            actor_user_id=message.requested_by,
            tenant_id=message.tenant_id,
            contact_stage=message.contact_stage,
            outcome=status,
            notes=notes,
            message_id=message.id,
            provider_verified=status == "sent",
        )
        session.commit()


async def _deliver_direct_message(
    message_id: uuid.UUID,
    *,
    provider: SmtpProvider,
    storage: ObjectStorage,
    semaphore: asyncio.Semaphore,
    session_factory,
) -> str:
    async with semaphore:
        try:
            with session_factory() as session:
                message = session.get(OutboundMessage, message_id)
                if message is None or message.status != "processing":
                    return message.status if message is not None else "failed"
                eligibility = message_eligibility_error(session, message)
                if eligibility:
                    _finalize_direct_message(
                        session_factory,
                        message_id,
                        "skipped",
                        code="not_eligible",
                        reason=eligibility,
                    )
                    return "skipped"
                snapshot = dict(message.sender_snapshot)
                subject = message.subject
                body_html = message.body_html
                body_text = message.body_text
                asset_manifest = list(message.asset_manifest or [])
                recipient_email = message.recipient_email

            email_message = await compose_message_snapshot(
                subject=subject,
                body_html=body_html,
                body_text=body_text,
                asset_manifest=asset_manifest,
                sender_name=str(snapshot["display_name"]),
                sender_email=str(snapshot["sender_email"]),
                recipient_email=recipient_email,
                reply_to=(
                    str(snapshot["reply_to"])
                    if snapshot.get("reply_to")
                    else None
                ),
                storage=storage,
            )
            if len(email_message.as_bytes()) > settings.mail_max_encoded_message_bytes:
                _finalize_direct_message(
                    session_factory,
                    message_id,
                    "failed",
                    code="message_too_large",
                    reason="Encoded message exceeds the configured size limit",
                )
                return "failed"

            with session_factory() as session:
                message = session.scalar(
                    select(OutboundMessage).where(
                        OutboundMessage.id == message_id
                    ).with_for_update()
                )
                if message is None or message.status != "processing":
                    return message.status if message is not None else "failed"
                eligibility = message_eligibility_error(session, message)
                if eligibility:
                    session.rollback()
                    _finalize_direct_message(
                        session_factory,
                        message_id,
                        "skipped",
                        code="not_eligible",
                        reason=eligibility,
                    )
                    return "skipped"
                sender = session.get(SenderAccount, message.sender_account_id)
                if (
                    sender is None
                    or not sender.is_active
                    or sender.sender_email != snapshot["sender_email"]
                    or sender.display_name != snapshot["display_name"]
                    or sender.reply_to != snapshot.get("reply_to")
                ):
                    session.rollback()
                    _finalize_direct_message(
                        session_factory,
                        message_id,
                        "failed",
                        code="sender_changed",
                        reason="Sender configuration changed before SMTP",
                    )
                    return "failed"
                delivery_config = sender_delivery_config(sender)
                message.provider_attempt_started_at = datetime.now(timezone.utc)
                session.commit()

            try:
                await asyncio.wait_for(
                    provider.send_message(delivery_config, email_message),
                    timeout=settings.mail_direct_send_timeout_seconds,
                )
            except (TimeoutError, asyncio.TimeoutError):
                _finalize_direct_message(
                    session_factory,
                    message_id,
                    "unknown",
                    code="smtp_timeout_unknown",
                    reason="SMTP operation timed out; acceptance is unknown",
                )
                return "unknown"
            except SmtpAmbiguousDeliveryError as exception:
                _finalize_direct_message(
                    session_factory,
                    message_id,
                    "unknown",
                    code=exception.code,
                    reason=exception.safe_message,
                )
                return "unknown"
            except (
                SmtpConnectionError,
                SmtpAuthenticationError,
                SmtpSecurityError,
                SmtpProviderError,
            ) as exception:
                _finalize_direct_message(
                    session_factory,
                    message_id,
                    "failed",
                    code=exception.code,
                    reason=exception.safe_message,
                )
                return "failed"

            _finalize_direct_message(session_factory, message_id, "sent")
            return "sent"
        except MailCompositionError:
            _finalize_direct_message(
                session_factory,
                message_id,
                "failed",
                code="composition_failed",
                reason="Stored message content or assets are unavailable",
            )
            return "failed"
        except ObjectStorageError:
            _finalize_direct_message(
                session_factory,
                message_id,
                "failed",
                code="asset_storage_unavailable",
                reason="Message assets are temporarily unavailable",
            )
            return "failed"
        except (
            CredentialDecryptionError,
            CredentialEncryptionNotConfiguredError,
        ):
            _finalize_direct_message(
                session_factory,
                message_id,
                "failed",
                code="credential_unavailable",
                reason="Sender credential could not be loaded",
            )
            return "failed"
        except Exception:
            _finalize_direct_message(
                session_factory,
                message_id,
                "failed",
                code="direct_send_failed",
                reason="Direct email processing failed",
            )
            return "failed"


async def deliver_bulk_direct(
    batch_id: uuid.UUID,
    *,
    provider: SmtpProvider,
    storage: ObjectStorage,
    session_factory,
) -> None:
    with session_factory() as session:
        message_ids = list(session.scalars(
            select(OutboundMessage.id).where(
                OutboundMessage.batch_id == batch_id,
                OutboundMessage.status == "processing",
            ).order_by(OutboundMessage.created_at, OutboundMessage.id)
        ).all())
    semaphore = asyncio.Semaphore(settings.mail_direct_send_concurrency)
    delivery = asyncio.gather(*(
        _deliver_direct_message(
            message_id,
            provider=provider,
            storage=storage,
            semaphore=semaphore,
            session_factory=session_factory,
        )
        for message_id in message_ids
    ))
    try:
        await asyncio.shield(delivery)
    except asyncio.CancelledError:
        await delivery
        raise
    finally:
        with session_factory() as session:
            batch = session.scalar(
                select(OutboundMessageBatch).where(
                    OutboundMessageBatch.id == batch_id
                ).with_for_update()
            )
            if batch is not None:
                statuses = list(session.scalars(
                    select(OutboundMessage.status).where(
                        OutboundMessage.batch_id == batch_id
                    )
                ).all())
                if any(status == "processing" for status in statuses):
                    batch.status = "processing"
                elif statuses and all(status == "sent" for status in statuses):
                    batch.status = "completed"
                elif statuses:
                    batch.status = "partially_completed"
                else:
                    batch.status = "completed"
                session.commit()


def cancel_bulk_batch(session: Session, context: WorkspaceContext, batch_id: uuid.UUID, reason: str) -> dict[str, int]:
    batch = get_batch(session, context.tenant.id, batch_id, for_update=True)
    if batch is None:
        raise BulkBatchNotFoundError
    now = datetime.now(timezone.utc)
    counts = {"cancelled": 0, "in_flight": 0, "already_terminal": 0}
    for recipient in all_batch_recipients(session, batch.id, for_update=True):
        if not recipient.message_id:
            continue
        message = session.get(OutboundMessage, recipient.message_id)
        job = get_job_for_message(session, recipient.message_id, for_update=True)
        if message is None or job is None:
            continue
        if message.status == "processing":
            message.cancel_requested_at = now
            message.cancellation_reason = reason
            counts["in_flight"] += 1
        elif message.status == "queued" and job.status in {"queued", "retry"}:
            message.status = "cancelled"
            message.cancelled_at = now
            message.cancelled_by = context.user.id
            message.cancellation_reason = reason
            job.status = "cancelled"
            add_event(session, message, "cancelled", actor_user_id=context.user.id, safe_reason=reason, occurred_at=now)
            counts["cancelled"] += 1
        else:
            counts["already_terminal"] += 1
    batch.cancelled_at = now
    batch.cancelled_by = context.user.id
    batch.status = "partially_completed" if counts["in_flight"] or counts["already_terminal"] else "cancelled"
    session.commit()
    return counts
