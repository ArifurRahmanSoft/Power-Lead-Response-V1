import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.lead_catalogs import LEAD_STATUS_TRANSITIONS, LeadStatus
from app.core.permissions import PermissionKey, RoleKey
from app.core.security import WorkspaceContext
from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead
from app.models.lead_activity import LeadActivity
from app.repositories.customer_repository import (
    CustomerPage,
    attach_customer_origins,
    get_conversion_by_lead,
    get_customer,
    get_customer_by_email,
    list_customer_conversions,
    list_customers,
)
from app.repositories.lead_engagement_repository import add_automation_stop_event
from app.repositories.lead_repository import (
    active_assignee_exists,
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
    CUSTOMER_WRITABLE_FIELDS,
    CustomerCreateRequest,
    CustomerDuplicateResolution,
    CustomerUpdateRequest,
    LeadConversionRequest,
)
from app.schemas.lead import LeadEngageRequest
from app.utils.lead_normalization import (
    country_name_to_code,
    normalize_country_name,
    normalize_optional_phone_number,
    normalize_optional_whatsapp_number,
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


class CustomerWriteConflictError(Exception):
    pass


class CustomerWritePermissionError(Exception):
    pass


class CustomerWriteValidationError(Exception):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(message)


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


def _validate_customer_assignee(
    database_session: Session,
    context: WorkspaceContext,
    assigned_user_id: uuid.UUID | None,
) -> None:
    if assigned_user_id is None:
        return
    if PermissionKey.LEADS_ASSIGN.value not in context.permissions:
        raise CustomerWritePermissionError
    if not active_assignee_exists(
        database_session,
        context.tenant.id,
        assigned_user_id,
    ):
        raise CustomerWriteValidationError(
            "assigned_user_id",
            "Assigned user must be an active member of this workspace",
        )


def create_customer(
    database_session: Session,
    context: WorkspaceContext,
    payload: CustomerCreateRequest,
) -> Customer:
    normalized_email = str(payload.email).lower()
    duplicate = get_customer_by_email(
        database_session,
        tenant_id=context.tenant.id,
        email=normalized_email,
    )
    if duplicate is not None:
        raise CustomerDuplicateError(duplicate)
    _validate_customer_assignee(
        database_session,
        context,
        payload.assigned_user_id,
    )

    values = payload.model_dump(mode="python")
    values["email"] = normalized_email
    values["country"] = country_name_to_code(values.get("country"))
    customer = Customer(
        **values,
        tenant_id=context.tenant.id,
        source_type="direct",
        communication_purpose="customer_relationship",
        channel_restrictions=[],
        created_by=context.user.id,
    )
    database_session.add(customer)
    try:
        database_session.flush()
        add_authorization_audit(
            database_session,
            tenant_id=context.tenant.id,
            actor_user_id=context.user.id,
            action="customer.created",
            target_membership_id=context.membership.id,
            details={
                "customer_id": str(customer.id),
                "source_type": "direct",
            },
        )
        database_session.commit()
        database_session.refresh(customer)
        attach_customer_origins(
            database_session,
            [customer],
            tenant_id=context.tenant.id,
        )
        return customer
    except IntegrityError as exception:
        database_session.rollback()
        duplicate = get_customer_by_email(
            database_session,
            tenant_id=context.tenant.id,
            email=normalized_email,
        )
        if duplicate is not None:
            raise CustomerDuplicateError(duplicate) from exception
        raise
    except Exception:
        database_session.rollback()
        raise


def update_customer(
    database_session: Session,
    context: WorkspaceContext,
    customer_id: uuid.UUID,
    payload: CustomerUpdateRequest,
) -> Customer:
    customer = get_customer(
        database_session,
        tenant_id=context.tenant.id,
        customer_id=customer_id,
        assigned_scope_user_id=_assigned_scope(context),
        for_update=True,
    )
    if customer is None:
        raise CustomerNotFoundError
    if customer.row_version != payload.row_version:
        database_session.rollback()
        raise CustomerWriteConflictError

    changes = payload.model_dump(
        exclude_unset=True,
        exclude={"row_version"},
        mode="python",
    )
    effective_country = (
        changes["country"] if "country" in changes else normalize_country_name(customer.country)
    )
    for field, normalizer in (
        ("phone", normalize_optional_phone_number),
        ("whatsapp_number", normalize_optional_whatsapp_number),
    ):
        if field in changes:
            try:
                changes[field] = normalizer(
                    changes[field],
                    country=effective_country,
                )
            except ValueError as exception:
                database_session.rollback()
                raise CustomerWriteValidationError(field, str(exception)) from exception
    if "country" in changes:
        changes["country"] = country_name_to_code(changes["country"])

    revenue = {
        field: changes.get(field, getattr(customer, field))
        for field in ("revenue_amount", "revenue_currency", "revenue_period")
    }
    if any(value is not None for value in revenue.values()) and not all(
        value is not None for value in revenue.values()
    ):
        database_session.rollback()
        raise CustomerWriteValidationError(
            "revenue_amount",
            "Revenue amount, currency, and period must be provided together",
        )

    if "assigned_user_id" in changes and changes["assigned_user_id"] != customer.assigned_user_id:
        if PermissionKey.LEADS_ASSIGN.value not in context.permissions:
            database_session.rollback()
            raise CustomerWritePermissionError
        try:
            _validate_customer_assignee(
                database_session,
                context,
                changes["assigned_user_id"],
            )
        except Exception:
            database_session.rollback()
            raise
    if "email" in changes:
        normalized_email = str(changes["email"]).lower()
        changes["email"] = normalized_email
        duplicate = get_customer_by_email(
            database_session,
            tenant_id=context.tenant.id,
            email=normalized_email,
        )
        if duplicate is not None and duplicate.id != customer.id:
            database_session.rollback()
            raise CustomerDuplicateError(duplicate)

    changed_fields = sorted(changes)
    now = datetime.now(timezone.utc)
    try:
        for field in CUSTOMER_WRITABLE_FIELDS:
            if field in changes:
                setattr(customer, field, changes[field])
        customer.row_version += 1
        customer.updated_at = now
        database_session.flush()
        add_authorization_audit(
            database_session,
            tenant_id=context.tenant.id,
            actor_user_id=context.user.id,
            action="customer.updated",
            target_membership_id=context.membership.id,
            details={
                "customer_id": str(customer.id),
                "changed_fields": changed_fields,
                "row_version": customer.row_version,
            },
        )
        database_session.commit()
        database_session.refresh(customer)
        attach_customer_origins(
            database_session,
            [customer],
            tenant_id=context.tenant.id,
            assigned_scope_user_id=_assigned_scope(context),
        )
        return customer
    except IntegrityError as exception:
        database_session.rollback()
        duplicate = get_customer_by_email(
            database_session,
            tenant_id=context.tenant.id,
            email=str(changes.get("email", customer.email)).lower(),
        )
        if duplicate is not None and duplicate.id != customer.id:
            raise CustomerDuplicateError(duplicate) from exception
        raise
    except Exception:
        database_session.rollback()
        raise


def _check_lead_scope(context: WorkspaceContext, lead: Lead) -> None:
    if PermissionKey.LEADS_VIEW.value not in context.permissions:
        raise CustomerConversionPermissionError
    if not _conversion_actor_allowed(context, lead):
        raise CustomerConversionPermissionError


def _conversion_actor_allowed(context: WorkspaceContext, lead: Lead) -> bool:
    return (
        PermissionKey.CUSTOMERS_CONVERT.value in context.permissions
        or (
            lead.assigned_user_id == context.user.id
            and PermissionKey.LEADS_UPDATE.value in context.permissions
        )
    )


def lead_conversion_capability(
    context: WorkspaceContext,
    lead: Lead,
) -> tuple[bool, str | None]:
    if lead.status == LeadStatus.CONVERTED.value:
        return False, "already_converted"
    if lead.deleted_at is not None:
        return False, "lead_archived"
    if lead.status == LeadStatus.DISQUALIFIED.value:
        return False, "lead_disqualified"
    if _conversion_actor_allowed(context, lead):
        return True, None
    return False, "conversion_not_authorized"


def lead_engagement_capability(
    context: WorkspaceContext,
    lead: Lead,
) -> tuple[bool, str | None]:
    if lead.status == LeadStatus.CONVERTED.value:
        return False, "already_converted"
    if lead.status == LeadStatus.ENGAGED.value:
        return False, "already_engaged"
    if lead.status != LeadStatus.CONTACTED.value:
        return False, "lead_must_be_contacted"
    if _conversion_actor_allowed(context, lead):
        return True, None
    return False, "engagement_not_authorized"


def lead_lifecycle_capabilities(
    context: WorkspaceContext,
    lead: Lead,
) -> dict[str, bool | str | None]:
    can_convert, conversion_reason = lead_conversion_capability(context, lead)
    can_engage, engagement_reason = lead_engagement_capability(context, lead)
    return {
        "can_convert_to_customer": can_convert,
        "conversion_blocking_reason": conversion_reason,
        "can_mark_engaged": can_engage,
        "engagement_blocking_reason": engagement_reason,
    }


def mark_lead_engaged(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    payload: LeadEngageRequest,
) -> Lead:
    lead = get_lead(database_session, context.tenant.id, lead_id, for_update=True)
    if lead is None:
        raise CustomerConversionLeadNotFoundError
    _check_lead_scope(context, lead)
    if lead.row_version != payload.row_version:
        raise CustomerConversionConflictError

    current = LeadStatus(lead.status)
    if LeadStatus.ENGAGED not in LEAD_STATUS_TRANSITIONS[current]:
        if current == LeadStatus.CONVERTED:
            raise CustomerConversionStateError(
                "Converted leads cannot be marked engaged"
            )
        if current == LeadStatus.ENGAGED:
            raise CustomerConversionStateError("Lead is already engaged")
        raise CustomerConversionStateError(
            "Only a contacted lead can be marked engaged"
        )

    changed_at = datetime.now(timezone.utc)
    try:
        if not update_lead_versioned(
            database_session,
            lead,
            payload.row_version,
            {
                "status": LeadStatus.ENGAGED.value,
                "updated_by": context.user.id,
                "updated_at": changed_at,
            },
        ):
            database_session.rollback()
            raise CustomerConversionConflictError
        database_session.expire_all()
        updated = get_lead(database_session, context.tenant.id, lead_id)
        if updated is None:
            database_session.rollback()
            raise CustomerConversionLeadNotFoundError
        add_lead_event(
            database_session,
            lead=updated,
            actor_user_id=context.user.id,
            event_type="lead.status_changed",
            details={
                "previous": current.value,
                "current": LeadStatus.ENGAGED.value,
                "reason": payload.reason,
                "changed_by": str(context.user.id),
                "changed_at": changed_at.isoformat(),
                "transition_source": "explicit_engagement",
            },
        )
        add_authorization_audit(
            database_session,
            tenant_id=context.tenant.id,
            actor_user_id=context.user.id,
            action="lead.engaged",
            target_membership_id=context.membership.id,
            details={
                "lead_id": str(lead_id),
                "previous_status": current.value,
                "new_status": LeadStatus.ENGAGED.value,
                "reason": payload.reason,
                "changed_at": changed_at.isoformat(),
            },
        )
        database_session.commit()
        database_session.refresh(updated)
        return updated
    except Exception:
        database_session.rollback()
        raise


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
        designation=lead.designation,
        country=lead.country,
        linkedin_url=lead.linkedin_url,
        facebook_url=lead.facebook_url,
        instagram_url=lead.instagram_url,
        x_url=lead.x_url,
        github_url=lead.github_url,
        phone=lead.phone,
        whatsapp_number=lead.whatsapp_number,
        company=lead.company,
        city=lead.city,
        state=lead.state,
        company_website=lead.company_website,
        industry=lead.industry,
        service_requested=lead.service_requested,
        company_size=lead.company_size,
        revenue_amount=lead.revenue_amount,
        revenue_currency=lead.revenue_currency,
        revenue_period=lead.revenue_period,
        notes=lead.notes,
        assigned_user_id=lead.assigned_user_id,
        source_type="lead",
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
    lead = get_lead(
        database_session,
        context.tenant.id,
        lead_id,
        include_archived=True,
        for_update=True,
    )
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
    if lead.deleted_at is not None:
        raise CustomerConversionStateError("Archived leads cannot be converted")
    if lead.status == LeadStatus.DISQUALIFIED.value:
        raise CustomerConversionStateError("Disqualified leads cannot be converted")
    if lead.status == LeadStatus.CONVERTED.value:
        raise CustomerConversionStateError("Lead is already converted")

    now = datetime.now(timezone.utc)
    lead_email = lead.email.lower()
    pending_task_reason = (
        payload.pending_task_reason or "Lead converted to customer"
    )
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
            pending_task_reason=pending_task_reason,
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
            reason=pending_task_reason,
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
        database_session.add(
            LeadActivity(
                tenant_id=context.tenant.id,
                lead_id=lead.id,
                activity_type="note",
                channel="other",
                direction="internal",
                occurred_at=now,
                performed_by=context.user.id,
                outcome="converted_to_customer",
                notes="Lead converted to customer through the explicit conversion workflow.",
                recorded_by=context.user.id,
                is_system=True,
                is_amendment=False,
                externally_performed=False,
                provider_verified=False,
            )
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
        attach_customer_origins(
            database_session,
            [customer],
            tenant_id=context.tenant.id,
        )
        return ConversionResult(customer, conversion, False)
    except IntegrityError as exception:
        database_session.rollback()
        existing_conversion = get_conversion_by_lead(
            database_session,
            tenant_id=context.tenant.id,
            lead_id=lead_id,
        )
        if existing_conversion is not None:
            return _load_conversion_result(
                database_session,
                tenant_id=context.tenant.id,
                conversion=existing_conversion,
                replay=True,
            )
        duplicate = get_customer_by_email(
            database_session,
            tenant_id=context.tenant.id,
            email=lead_email,
        )
        if duplicate is not None:
            raise CustomerDuplicateError(duplicate) from exception
        raise
    except Exception:
        database_session.rollback()
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
