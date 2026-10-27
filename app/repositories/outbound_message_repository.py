import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.outbound_message import (
    OutboundBatchRecipient,
    OutboundMessage,
    OutboundMessageBatch,
    OutboundMessageEvent,
    OutboundMessageJob,
    OutboundMessagePreview,
)


@dataclass(frozen=True)
class OutboundMessagePage:
    items: list[OutboundMessage]
    total: int


@dataclass(frozen=True)
class BatchRecipientPage:
    items: list[OutboundBatchRecipient]
    total: int


def get_batch(session: Session, tenant_id: uuid.UUID, batch_id: uuid.UUID, *, for_update: bool = False) -> OutboundMessageBatch | None:
    statement = select(OutboundMessageBatch).where(
        OutboundMessageBatch.id == batch_id,
        OutboundMessageBatch.tenant_id == tenant_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def get_batch_by_idempotency(session: Session, tenant_id: uuid.UUID, key: str) -> OutboundMessageBatch | None:
    return session.scalar(select(OutboundMessageBatch).where(
        OutboundMessageBatch.tenant_id == tenant_id,
        OutboundMessageBatch.idempotency_key == key,
    ))


def list_batch_recipients(session: Session, batch_id: uuid.UUID, *, page: int, size: int) -> BatchRecipientPage:
    total = session.scalar(select(func.count(OutboundBatchRecipient.id)).where(
        OutboundBatchRecipient.batch_id == batch_id
    )) or 0
    items = list(session.scalars(select(OutboundBatchRecipient).where(
        OutboundBatchRecipient.batch_id == batch_id
    ).order_by(OutboundBatchRecipient.ordinal, OutboundBatchRecipient.id)
      .offset((page - 1) * size).limit(size)).all())
    return BatchRecipientPage(items, total)


def all_batch_recipients(session: Session, batch_id: uuid.UUID, *, for_update: bool = False) -> list[OutboundBatchRecipient]:
    statement = select(OutboundBatchRecipient).where(
        OutboundBatchRecipient.batch_id == batch_id
    ).order_by(OutboundBatchRecipient.ordinal, OutboundBatchRecipient.id)
    if for_update:
        statement = statement.with_for_update()
    return list(session.scalars(statement).all())


def active_message_for_lead(session: Session, tenant_id: uuid.UUID, lead_id: uuid.UUID, *, exclude_batch_id: uuid.UUID | None = None) -> OutboundMessage | None:
    statement = select(OutboundMessage).where(
        OutboundMessage.tenant_id == tenant_id,
        OutboundMessage.lead_id == lead_id,
        OutboundMessage.status.in_(("queued", "processing")),
    )
    if exclude_batch_id is not None:
        statement = statement.where(
            or_(OutboundMessage.batch_id.is_(None), OutboundMessage.batch_id != exclude_batch_id)
        )
    return session.scalar(statement.order_by(OutboundMessage.created_at.desc()).limit(1))


def get_preview(session: Session, tenant_id: uuid.UUID, preview_id: uuid.UUID, *, for_update: bool = False) -> OutboundMessagePreview | None:
    statement = select(OutboundMessagePreview).where(
        OutboundMessagePreview.id == preview_id,
        OutboundMessagePreview.tenant_id == tenant_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def get_by_idempotency(session: Session, tenant_id: uuid.UUID, key: str) -> OutboundMessage | None:
    return session.scalar(select(OutboundMessage).where(
        OutboundMessage.tenant_id == tenant_id,
        OutboundMessage.idempotency_key == key,
    ))


def get_message(session: Session, tenant_id: uuid.UUID, message_id: uuid.UUID, *, for_update: bool = False) -> OutboundMessage | None:
    statement = select(OutboundMessage).where(
        OutboundMessage.id == message_id,
        OutboundMessage.tenant_id == tenant_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def get_job_for_message(session: Session, message_id: uuid.UUID, *, for_update: bool = False) -> OutboundMessageJob | None:
    statement = select(OutboundMessageJob).where(OutboundMessageJob.message_id == message_id)
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def list_events(session: Session, message_id: uuid.UUID) -> list[OutboundMessageEvent]:
    return list(session.scalars(select(OutboundMessageEvent).where(
        OutboundMessageEvent.message_id == message_id
    ).order_by(OutboundMessageEvent.occurred_at, OutboundMessageEvent.id)).all())


def list_messages(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    page: int,
    size: int,
    lead_id: uuid.UUID | None,
    customer_id: uuid.UUID | None,
    status: str | None,
    assigned_scope_user_id: uuid.UUID | None,
) -> OutboundMessagePage:
    conditions = [OutboundMessage.tenant_id == tenant_id]
    if lead_id:
        conditions.append(OutboundMessage.lead_id == lead_id)
    if customer_id:
        conditions.append(OutboundMessage.customer_id == customer_id)
    if status:
        conditions.append(OutboundMessage.status == status)
    statement = select(OutboundMessage).where(*conditions)
    if assigned_scope_user_id is not None:
        from app.models.customer import LeadConversion
        from app.models.lead import Lead
        visible_leads = select(Lead.id).where(
            Lead.tenant_id == tenant_id,
            Lead.assigned_user_id == assigned_scope_user_id,
        )
        visible_customers = select(LeadConversion.customer_id).join(
            Lead, Lead.id == LeadConversion.lead_id
        ).where(
            LeadConversion.tenant_id == tenant_id,
            Lead.assigned_user_id == assigned_scope_user_id,
        )
        statement = statement.where(or_(
            OutboundMessage.lead_id.in_(visible_leads),
            OutboundMessage.customer_id.in_(visible_customers),
        ))
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    items = list(session.scalars(statement.order_by(
        OutboundMessage.created_at.desc(), OutboundMessage.id.desc()
    ).offset((page - 1) * size).limit(size)).all())
    return OutboundMessagePage(items, total)


def recent_sent_count(session: Session, *, tenant_id: uuid.UUID, since: datetime, sender_account_id: uuid.UUID | None = None) -> int:
    conditions = [
        OutboundMessage.tenant_id == tenant_id,
        OutboundMessage.sent_at >= since,
    ]
    if sender_account_id is not None:
        conditions.append(OutboundMessage.sender_account_id == sender_account_id)
    return session.scalar(select(func.count(OutboundMessage.id)).where(*conditions)) or 0


def recent_active_count(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    since: datetime,
    sender_account_id: uuid.UUID | None = None,
) -> int:
    conditions = [
        OutboundMessage.tenant_id == tenant_id,
        OutboundMessage.created_at >= since,
        OutboundMessage.status.in_(("queued", "processing")),
    ]
    if sender_account_id is not None:
        conditions.append(OutboundMessage.sender_account_id == sender_account_id)
    return session.scalar(
        select(func.count(OutboundMessage.id)).where(*conditions)
    ) or 0


def pending_worker_job_count(session: Session, *, tenant_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.count(OutboundMessageJob.id))
        .join(OutboundMessage, OutboundMessage.id == OutboundMessageJob.message_id)
        .where(
            OutboundMessage.tenant_id == tenant_id,
            OutboundMessageJob.status.in_(("queued", "retry", "processing")),
        )
    ) or 0


def add_event(session: Session, message: OutboundMessage, status: str, *, actor_user_id: uuid.UUID | None = None, safe_reason: str | None = None, occurred_at: datetime | None = None) -> OutboundMessageEvent:
    event = OutboundMessageEvent(
        tenant_id=message.tenant_id,
        message_id=message.id,
        status=status,
        actor_user_id=actor_user_id,
        safe_reason=safe_reason,
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )
    session.add(event)
    return event


def cancel_pending_acquisition_messages(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    reason: str,
    now: datetime,
) -> tuple[int, int]:
    messages = list(session.scalars(select(OutboundMessage).where(
        OutboundMessage.tenant_id == tenant_id,
        OutboundMessage.lead_id == lead_id,
        OutboundMessage.purpose == "lead_acquisition",
        OutboundMessage.status.in_(("queued", "processing")),
    ).with_for_update()).all())
    cancelled = in_flight = 0
    for message in messages:
        job = get_job_for_message(session, message.id, for_update=True)
        if message.status == "processing":
            message.cancel_requested_at = now
            message.cancellation_reason = reason
            in_flight += 1
            continue
        message.status = "cancelled"
        message.cancelled_at = now
        message.cancelled_by = actor_user_id
        message.cancellation_reason = reason
        if job:
            job.status = "cancelled"
        add_event(session, message, "cancelled", actor_user_id=actor_user_id, safe_reason=reason, occurred_at=now)
        cancelled += 1
    return cancelled, in_flight
