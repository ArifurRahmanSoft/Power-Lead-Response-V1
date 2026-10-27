import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.lead_catalogs import LeadStatus
from app.core.permissions import PermissionKey, RoleKey
from app.core.security import WorkspaceContext
from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead
from app.repositories.customer_repository import (
    CustomerPage,
    get_conversion_by_lead,
    get_customer,
    get_customer_by_email,
    list_customer_conversions,
    list_customers,
)
from app.repositories.lead_engagement_repository import add_automation_stop_event
from app.repositories.lead_repository import (
    add_lead_event,
    cancel_pending_customer_actions,
    get_lead,
    update_lead_versioned,
)
from app.repositories.lead_task_repository import lock_open_tasks_for_lead
from app.repositories.outbound_message_repository import cancel_pending_acquisition_messages
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.customer import (
    AcquisitionTaskDisposition,
    CustomerDuplicateResolution,
    LeadConversionRequest,
)


class CustomerNotFoundError(Exception):
    pass


class CustomerConversionLeadNotFoundError(Exception):
    pass


class CustomerConversionPermissionError(Exception):
    pass


class CustomerConversionConflictError(Exception):
    pass


class CustomerConversionStateError(Exception):
    pass


class CustomerDuplicateError(Exception):
    def __init__(self, customer: Customer) -> None:
        self.customer = customer


@dataclass(frozen=True)
class ConversionResult:
    customer: Customer
    conversion: LeadConversion
    idempotent_replay: bool


@dataclass(frozen=True)
class CustomerDetail:
    customer: Customer
    conversions: list[LeadConversion]


def _assigned_scope(context: WorkspaceContext) -> uuid.UUID | None:
    return context.user.id if context.role == RoleKey.STAFF.value else None


def _check_lead_scope(context: WorkspaceContext, lead: Lead) -> None:
    if PermissionKey.LEADS_VIEW.value not in context.permissions:
        raise CustomerConversionPermissionError
    if context.role == RoleKey.STAFF.value and lead.assigned_user_id != context.user.id:
        raise CustomerConversionPermissionError


def _load_conversion_result(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    conversion: LeadConversion,
    replay: bool,
) -> ConversionResult:
    customer = get_customer(
        database_session,
        tenant_id=tenant_id,
        customer_id=conversion.customer_id,
    )
    if customer is None:
        raise CustomerNotFoundError
    return ConversionResult(customer, conversion, replay)


def _resolve_customer(
    database_session: Session,
    context: WorkspaceContext,
    lead: Lead,
    payload: LeadConversionRequest,
) -> Customer:
    if payload.duplicate_resolution == CustomerDuplicateResolution.LINK_EXISTING:
        customer = get_customer(
            database_session,
            tenant_id=context.tenant.id,
            customer_id=payload.existing_customer_id,
            for_update=True,
        )
        if customer is None:
            raise CustomerNotFoundError
        return customer

    duplicate = get_customer_by_email(
        database_session,
        tenant_id=context.tenant.id,
        email=lead.email.lower(),
        for_update=True,
    )
    if duplicate is not None:
        raise CustomerDuplicateError(duplicate)
    customer = Customer(
        tenant_id=context.tenant.id,
        first_name=lead.first_name,
        last_name=lead.last_name,
        email=lead.email.lower(),
        phone=lead.phone,
        company=lead.company,
        # Lead-acquisition consent is intentionally not promoted to the separate
        # customer-relationship communication purpose.
        communication_purpose="customer_relationship",
        communication_consent_status="unknown",
        channel_restrictions=[],
        created_by=context.user.id,
    )
    database_session.add(customer)
    database_session.flush()
    return customer


def _review_acquisition_tasks(
    database_session: Session,
    context: WorkspaceContext,
    lead: Lead,
    *,
    disposition: AcquisitionTaskDisposition,
    reason: str,
    now: datetime,
) -> int:
    tasks = [
        task
        for task in lock_open_tasks_for_lead(
            database_session,
            tenant_id=context.tenant.id,
            lead_id=lead.id,
        )
        if task.task_type in {"contact", "follow_up"}
    ]
    if disposition == AcquisitionTaskDisposition.RETAIN:
        return len(tasks)
    for task in tasks:
        task.status = "cancelled"
        task.cancelled_by = context.user.id
        task.cancelled_at = now
        task.cancellation_reason = reason
        task.updated_by = context.user.id
        task.updated_at = now
        task.row_version += 1
    return len(tasks)


def convert_lead_to_customer(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    payload: LeadConversionRequest,
) -> ConversionResult:
    lead = get_lead(database_session, context.tenant.id, lead_id, for_update=True)
    if lead is None:
        raise CustomerConversionLeadNotFoundError
    _check_lead_scope(context, lead)

    existing_conversion = get_conversion_by_lead(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead.id,
    )
    if existing_conversion is not None:
        return _load_conversion_result(
            database_session,
            tenant_id=context.tenant.id,
            conversion=existing_conversion,
            replay=True,
        )
    if lead.row_version != payload.row_version:
        raise CustomerConversionConflictError
    if lead.status != LeadStatus.ENGAGED.value:
        raise CustomerConversionStateError(
            "Only an engaged lead can be explicitly converted"
        )

    now = datetime.now(timezone.utc)
    try:
        customer = _resolve_customer(database_session, context, lead, payload)
        conversion = LeadConversion(
            tenant_id=context.tenant.id,
            lead_id=lead.id,
            customer_id=customer.id,
            converted_at=now,
            converted_by=context.user.id,
            purchased_service=payload.purchased_service,
            reference=payload.reference,
            reason=payload.reason,
            duplicate_resolution=payload.duplicate_resolution.value,
            pending_task_disposition=payload.pending_acquisition_task_disposition.value,
            pending_task_reason=payload.pending_task_reason,
            source_consent_status=lead.consent_status,
            source_consent_evidence=lead.consent_evidence,
            source_consent_reference=lead.consent_reference,
        )
        database_session.add(conversion)
        database_session.flush()

        affected_tasks = _review_acquisition_tasks(
            database_session,
            context,
            lead,
            disposition=payload.pending_acquisition_task_disposition,
            reason=payload.pending_task_reason,
            now=now,
        )
        cancelled_jobs = cancel_pending_customer_actions(
            database_session,
            lead.id,
            now,
            cancelled_by=context.user.id,
            cancellation_reason="Lead converted to customer",
        )
        cancelled_messages, in_flight_messages = cancel_pending_acquisition_messages(
            database_session,
            tenant_id=context.tenant.id,
            lead_id=lead.id,
            actor_user_id=context.user.id,
            reason="Lead converted to customer",
            now=now,
        )
        conversion.affected_task_count = affected_tasks
        conversion.cancelled_nurture_job_count = cancelled_jobs

        for stop_key in ("lead_nurturing", "recovery_follow_up"):
            add_automation_stop_event(
                database_session,
                tenant_id=context.tenant.id,
                lead_id=lead.id,
                stop_key=stop_key,
                trigger_type="lead_converted",
                trigger_id=conversion.id,
                recorded_by=context.user.id,
            )

        database_session.flush()
        if not update_lead_versioned(
            database_session,
            lead,
            payload.row_version,
            {
                "status": LeadStatus.CONVERTED.value,
                "updated_by": context.user.id,
                "updated_at": now,
            },
        ):
            database_session.rollback()
            raise CustomerConversionConflictError

        add_lead_event(
            database_session,
            lead=lead,
            actor_user_id=context.user.id,
            event_type="lead.converted",
            details={
                "customer_id": str(customer.id),
                "conversion_id": str(conversion.id),
                "purchased_service": payload.purchased_service,
                "reference": payload.reference,
                "reason": payload.reason,
                "duplicate_resolution": payload.duplicate_resolution.value,
                "pending_task_disposition": payload.pending_acquisition_task_disposition.value,
                "affected_task_count": affected_tasks,
                "cancelled_nurture_job_count": cancelled_jobs,
                "cancelled_outbound_message_count": cancelled_messages,
                "in_flight_outbound_message_count": in_flight_messages,
            },
        )
        add_authorization_audit(
            database_session,
            tenant_id=context.tenant.id,
            actor_user_id=context.user.id,
            action="lead.converted",
            target_membership_id=context.membership.id,
            details={
                "lead_id": str(lead.id),
                "customer_id": str(customer.id),
                "conversion_id": str(conversion.id),
                "duplicate_resolution": payload.duplicate_resolution.value,
                "pending_task_disposition": payload.pending_acquisition_task_disposition.value,
            },
        )
        database_session.commit()
        database_session.refresh(customer)
        database_session.refresh(conversion)
        return ConversionResult(customer, conversion, False)
    except IntegrityError as exception:
        database_session.rollback()
        duplicate = get_customer_by_email(
            database_session,
            tenant_id=context.tenant.id,
            email=lead.email.lower(),
        )
        if duplicate is not None:
            raise CustomerDuplicateError(duplicate) from exception
        raise


def get_customers(
    database_session: Session,
    context: WorkspaceContext,
    *,
    page: int,
    size: int,
    search: str | None,
) -> CustomerPage:
    return list_customers(
        database_session,
        tenant_id=context.tenant.id,
        page=page,
        size=size,
        search=search,
        assigned_scope_user_id=_assigned_scope(context),
    )


def get_customer_detail(
    database_session: Session,
    context: WorkspaceContext,
    customer_id: uuid.UUID,
) -> CustomerDetail:
    customer = get_customer(
        database_session,
        tenant_id=context.tenant.id,
        customer_id=customer_id,
        assigned_scope_user_id=_assigned_scope(context),
    )
    if customer is None:
        raise CustomerNotFoundError
    return CustomerDetail(
        customer,
        list_customer_conversions(
            database_session,
            tenant_id=context.tenant.id,
            customer_id=customer.id,
            assigned_scope_user_id=_assigned_scope(context),
        ),
    )
