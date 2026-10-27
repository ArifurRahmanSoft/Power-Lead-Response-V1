import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models.lead import (
    Lead,
    LeadCustomerAction,
    LeadEvent,
    LeadImportBatch,
    LeadImportRow,
)
from app.models.membership import TenantMembership
from app.models.user import User
from app.models.outbound_message import OutboundMessage
from app.models.sms_message import SmsMessage
from app.schemas.lead import LeadFilterParams
from app.utils.lead_normalization import country_name_to_code


@dataclass(frozen=True)
class Page:
    items: list[Any]
    total: int


@dataclass(frozen=True)
class ServiceOptionRecord:
    label: str
    value: str


def get_active_duplicate(
    database_session: Session,
    tenant_id: uuid.UUID,
    email: str,
    *,
    exclude_lead_id: uuid.UUID | None = None,
) -> Lead | None:
    statement = select(Lead).where(
        Lead.tenant_id == tenant_id,
        Lead.email == email,
        Lead.deleted_at.is_(None),
    )
    if exclude_lead_id is not None:
        statement = statement.where(Lead.id != exclude_lead_id)
    return database_session.scalar(statement)


def get_existing_active_emails(
    database_session: Session,
    tenant_id: uuid.UUID,
    emails: set[str],
) -> set[str]:
    if not emails:
        return set()
    return set(
        database_session.scalars(
            select(Lead.email).where(
                Lead.tenant_id == tenant_id,
                Lead.email.in_(emails),
                Lead.deleted_at.is_(None),
            )
        ).all()
    )


def get_lead(
    database_session: Session,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    *,
    include_archived: bool = False,
    for_update: bool = False,
) -> Lead | None:
    statement = select(Lead).where(Lead.tenant_id == tenant_id, Lead.id == lead_id)
    if not include_archived:
        statement = statement.where(Lead.deleted_at.is_(None))
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def lead_filter_conditions(
    *,
    tenant_id: uuid.UUID,
    filters: LeadFilterParams,
    assigned_scope_user_id: uuid.UUID | None,
) -> list[Any]:
    conditions: list[Any] = [Lead.tenant_id == tenant_id, Lead.deleted_at.is_(None)]
    if assigned_scope_user_id is not None:
        conditions.append(Lead.assigned_user_id == assigned_scope_user_id)
    if filters.search:
        term = f"%{filters.search.strip()}%"
        conditions.append(or_(Lead.first_name.ilike(term), Lead.last_name.ilike(term), Lead.email.ilike(term), Lead.company.ilike(term)))
    for field in ("email", "company", "industry"):
        value = getattr(filters, field)
        if value:
            conditions.append(getattr(Lead, field).ilike(f"%{value.strip()}%"))
    if filters.service_requested:
        conditions.append(func.lower(func.trim(Lead.service_requested)) == filters.service_requested.lower())
    if filters.country:
        conditions.append(Lead.country == country_name_to_code(filters.country))
    if filters.name:
        term = f"%{filters.name.strip()}%"
        conditions.append(or_(Lead.first_name.ilike(term), Lead.last_name.ilike(term)))
    for field in ("status", "email_status", "source", "priority"):
        value = getattr(filters, field)
        if value is not None:
            conditions.append(getattr(Lead, field) == str(value))
    if filters.assigned_user_id is not None:
        conditions.append(Lead.assigned_user_id == filters.assigned_user_id)
    latest_state = select(OutboundMessage.status).where(
        OutboundMessage.tenant_id == tenant_id,
        OutboundMessage.lead_id == Lead.id,
    ).order_by(OutboundMessage.created_at.desc(), OutboundMessage.id.desc()).limit(1).correlate(Lead).scalar_subquery()
    latest_stage = select(OutboundMessage.contact_stage).where(
        OutboundMessage.tenant_id == tenant_id,
        OutboundMessage.lead_id == Lead.id,
    ).order_by(OutboundMessage.created_at.desc(), OutboundMessage.id.desc()).limit(1).correlate(Lead).scalar_subquery()
    if filters.email_state:
        conditions.append(func.coalesce(latest_state, "not_contacted_by_email") == filters.email_state)
    if filters.email_stage:
        conditions.append(latest_stage == filters.email_stage)
    return conditions


def list_authorized_lead_ids(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    filters: LeadFilterParams,
    assigned_scope_user_id: uuid.UUID | None,
    limit: int,
) -> tuple[list[uuid.UUID], int]:
    conditions = lead_filter_conditions(
        tenant_id=tenant_id,
        filters=filters,
        assigned_scope_user_id=assigned_scope_user_id,
    )
    total = database_session.scalar(select(func.count(Lead.id)).where(*conditions)) or 0
    ids = list(database_session.scalars(
        select(Lead.id).where(*conditions).order_by(Lead.created_at.desc(), Lead.id).limit(limit)
    ).all())
    return ids, total


def attach_email_summaries(database_session: Session, leads: list[Lead]) -> None:
    if not leads:
        return
    lead_ids = [lead.id for lead in leads]
    ranked = select(
        OutboundMessage.lead_id.label("lead_id"),
        OutboundMessage.status.label("state"),
        OutboundMessage.contact_stage.label("stage"),
        OutboundMessage.provider_attempt_started_at.label("attempted_at"),
        OutboundMessage.sent_at.label("sent_at"),
        func.row_number().over(
            partition_by=OutboundMessage.lead_id,
            order_by=(OutboundMessage.created_at.desc(), OutboundMessage.id.desc()),
        ).label("position"),
    ).where(OutboundMessage.lead_id.in_(lead_ids)).subquery()
    aggregates = select(
        OutboundMessage.lead_id.label("lead_id"),
        func.count(OutboundMessage.id).label("attempt_count"),
        func.max(OutboundMessage.sent_at).label("last_successful_email_at"),
    ).where(OutboundMessage.lead_id.in_(lead_ids)).group_by(OutboundMessage.lead_id).subquery()
    rows = database_session.execute(
        select(ranked, aggregates.c.attempt_count, aggregates.c.last_successful_email_at)
        .join(aggregates, aggregates.c.lead_id == ranked.c.lead_id)
        .where(ranked.c.position == 1)
    ).mappings().all()
    by_lead = {row["lead_id"]: dict(row) for row in rows}
    for lead in leads:
        summary = by_lead.get(lead.id)
        lead.email_summary = ({
            "state": summary["state"],
            "stage": summary["stage"],
            "attempted_at": summary["attempted_at"],
            "sent_at": summary["sent_at"],
            "last_successful_email_at": summary["last_successful_email_at"],
            "attempt_count": summary["attempt_count"],
        } if summary else {"state": "not_contacted_by_email", "stage": None, "attempt_count": 0})


def attach_sms_summaries(database_session: Session, leads: list[Lead]) -> None:
    if not leads:
        return
    lead_ids = [lead.id for lead in leads]
    ranked = select(
        SmsMessage.lead_id.label("lead_id"),
        SmsMessage.status.label("provider_status"),
        SmsMessage.contact_type.label("contact_type"),
        func.row_number().over(
            partition_by=SmsMessage.lead_id,
            order_by=(SmsMessage.created_at.desc(), SmsMessage.id.desc()),
        ).label("position"),
    ).where(SmsMessage.lead_id.in_(lead_ids)).subquery()
    successful = select(
        SmsMessage.lead_id.label("lead_id"),
        func.max(func.coalesce(SmsMessage.delivered_at, SmsMessage.sent_at)).label(
            "last_successful_at"
        ),
    ).where(
        SmsMessage.lead_id.in_(lead_ids),
        SmsMessage.status.in_(("sent", "delivered")),
    ).group_by(SmsMessage.lead_id).subquery()
    rows = database_session.execute(
        select(ranked, successful.c.last_successful_at)
        .outerjoin(successful, successful.c.lead_id == ranked.c.lead_id)
        .where(ranked.c.position == 1)
    ).mappings().all()
    by_lead = {row["lead_id"]: dict(row) for row in rows}
    for lead in leads:
        summary = by_lead.get(lead.id)
        lead.sms_summary = (
            {
                "contact_type": summary["contact_type"],
                "provider_status": summary["provider_status"],
                "last_successful_at": summary["last_successful_at"],
            }
            if summary
            else {
                "contact_type": None,
                "provider_status": "not_contacted_by_sms",
                "last_successful_at": None,
            }
        )


def list_leads(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    page: int,
    size: int,
    filters: LeadFilterParams,
    assigned_scope_user_id: uuid.UUID | None,
) -> Page:
    conditions = lead_filter_conditions(
        tenant_id=tenant_id,
        filters=filters,
        assigned_scope_user_id=assigned_scope_user_id,
    )

    total = database_session.scalar(select(func.count(Lead.id)).where(*conditions)) or 0
    items = list(
        database_session.scalars(
            select(Lead)
            .where(*conditions)
            .order_by(Lead.created_at.desc(), Lead.id)
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    attach_email_summaries(database_session, items)
    attach_sms_summaries(database_session, items)
    return Page(items=items, total=total)


def list_service_options(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    page: int,
    size: int,
    search: str | None,
    assigned_scope_user_id: uuid.UUID | None,
) -> Page:
    trimmed_service = func.trim(Lead.service_requested)
    normalized_service = func.lower(trimmed_service)
    conditions = [
        Lead.tenant_id == tenant_id,
        Lead.deleted_at.is_(None),
        Lead.service_requested.is_not(None),
        func.length(trimmed_service) > 0,
    ]
    if assigned_scope_user_id is not None:
        conditions.append(Lead.assigned_user_id == assigned_scope_user_id)
    if search and search.strip():
        conditions.append(
            normalized_service.contains(search.strip().lower(), autoescape=True)
        )

    grouped = (
        select(
            normalized_service.label("value"),
            func.min(trimmed_service).label("label"),
        )
        .where(*conditions)
        .group_by(normalized_service)
    )
    total = database_session.scalar(
        select(func.count()).select_from(grouped.subquery())
    ) or 0
    rows = database_session.execute(
        grouped
        .order_by(normalized_service.asc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return Page(
        items=[ServiceOptionRecord(label=row.label, value=row.value) for row in rows],
        total=total,
    )


def active_assignee_exists(
    database_session: Session,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    return (
        database_session.scalar(
            select(TenantMembership.id)
            .join(User, User.id == TenantMembership.user_id)
            .where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.user_id == user_id,
                TenantMembership.is_active.is_(True),
                TenantMembership.revoked_at.is_(None),
                User.is_active.is_(True),
            )
            .limit(1)
        )
        is not None
    )


def update_lead_versioned(
    database_session: Session,
    lead: Lead,
    expected_version: int,
    values: dict[str, Any],
) -> bool:
    result = database_session.execute(
        update(Lead)
        .where(
            Lead.id == lead.id,
            Lead.tenant_id == lead.tenant_id,
            Lead.deleted_at.is_(None),
            Lead.row_version == expected_version,
        )
        .values(**values, row_version=Lead.row_version + 1)
        .returning(Lead.id)
    )
    return result.scalar_one_or_none() is not None


def add_lead_event(
    database_session: Session,
    *,
    lead: Lead,
    actor_user_id: uuid.UUID,
    event_type: str,
    details: dict[str, Any],
) -> None:
    database_session.add(
        LeadEvent(
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            details=details,
        )
    )


def cancel_pending_customer_actions(
    database_session: Session,
    lead_id: uuid.UUID,
    cancelled_at: datetime,
    *,
    cancelled_by: uuid.UUID | None = None,
    cancellation_reason: str | None = None,
) -> int:
    result = database_session.execute(
        update(LeadCustomerAction)
        .where(
            LeadCustomerAction.lead_id == lead_id,
            LeadCustomerAction.status == "pending",
        )
        .values(
            status="cancelled",
            cancelled_at=cancelled_at,
            cancelled_by=cancelled_by,
            cancellation_reason=cancellation_reason,
        )
    )
    return int(result.rowcount or 0)


def get_import_batch(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    batch_id: uuid.UUID,
    for_update: bool = False,
) -> LeadImportBatch | None:
    statement = select(LeadImportBatch).where(
        LeadImportBatch.tenant_id == tenant_id,
        LeadImportBatch.id == batch_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def get_import_batch_by_hash(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    snapshot_hash: str,
) -> LeadImportBatch | None:
    return database_session.scalar(
        select(LeadImportBatch).where(
            LeadImportBatch.tenant_id == tenant_id,
            LeadImportBatch.created_by == created_by,
            LeadImportBatch.snapshot_hash == snapshot_hash,
        )
    )


def list_import_rows(
    database_session: Session,
    batch_id: uuid.UUID,
    *,
    statuses: set[str] | None = None,
) -> list[LeadImportRow]:
    statement = select(LeadImportRow).where(LeadImportRow.batch_id == batch_id)
    if statuses:
        statement = statement.where(LeadImportRow.status.in_(statuses))
    return list(database_session.scalars(statement.order_by(LeadImportRow.row_number)).all())


def list_import_error_rows(
    database_session: Session,
    batch_id: uuid.UUID,
    *,
    page: int,
    size: int,
) -> Page:
    conditions = (
        LeadImportRow.batch_id == batch_id,
        LeadImportRow.status.in_({"invalid", "duplicate", "failed"}),
    )
    total = database_session.scalar(
        select(func.count(LeadImportRow.id)).where(*conditions)
    ) or 0
    items = list(
        database_session.scalars(
            select(LeadImportRow)
            .where(*conditions)
            .order_by(LeadImportRow.row_number)
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    return Page(items=items, total=total)
