import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.lead_catalogs import ConsentStatus, LEAD_STATUS_TRANSITIONS, LeadSource, LeadStatus
from app.core.permissions import PermissionKey, RoleKey
from app.core.security import WorkspaceContext
from app.models.lead import Lead
from app.repositories.lead_repository import (
    Page,
    active_assignee_exists,
    add_lead_event,
    cancel_pending_customer_actions,
    get_active_duplicate,
    get_lead,
    list_leads,
    list_service_options,
    update_lead_versioned,
)
from app.repositories.lead_assignment_repository import (
    AssignmentHistoryPage,
    add_assignment_history,
    list_assignment_history,
)
from app.repositories.lead_task_repository import lock_open_tasks_for_lead
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.lead import LeadCreateRequest, LeadFields, LeadFilterParams, LeadUpdateRequest
from app.utils.lead_normalization import country_name_to_code


class LeadNotFoundError(Exception):
    pass


class LeadDuplicateError(Exception):
    def __init__(self, existing: Lead) -> None:
        self.existing = existing


class LeadConflictError(Exception):
    pass


class LeadAssignmentError(Exception):
    pass


class LeadAssignmentDispositionError(Exception):
    pass


class LeadTransitionError(Exception):
    pass


class LeadExplicitConversionRequiredError(Exception):
    pass


class LeadScopeError(Exception):
    pass


class LeadPermissionError(Exception):
    pass


def _tracking_id() -> str:
    return f"LD-{uuid.uuid4().hex.upper()}"


def _is_staff(context: WorkspaceContext) -> bool:
    return context.role == RoleKey.STAFF.value


def _enforce_scope(context: WorkspaceContext, lead: Lead, *, write: bool) -> None:
    if lead.tenant_id != context.tenant.id:
        raise LeadNotFoundError
    if _is_staff(context) and lead.assigned_user_id != context.user.id:
        raise LeadScopeError
    permission = PermissionKey.LEADS_UPDATE.value if write else PermissionKey.LEADS_VIEW.value
    if permission not in context.permissions:
        raise LeadPermissionError


def _validate_assignee(
    database_session: Session,
    context: WorkspaceContext,
    assigned_user_id: uuid.UUID | None,
) -> None:
    if assigned_user_id is None:
        return
    if not active_assignee_exists(database_session, context.tenant.id, assigned_user_id):
        raise LeadAssignmentError


def _validate_transition(current: str, requested: LeadStatus) -> None:
    current_status = LeadStatus(current)
    if requested == current_status:
        return
    if requested not in LEAD_STATUS_TRANSITIONS[current_status]:
        raise LeadTransitionError


def create_lead(
    database_session: Session,
    context: WorkspaceContext,
    payload: LeadCreateRequest,
) -> Lead:
    normalized_email = str(payload.email).lower()
    duplicate = get_active_duplicate(database_session, context.tenant.id, normalized_email)
    if duplicate is not None:
        raise LeadDuplicateError(duplicate)
    if payload.assigned_user_id is not None:
        if PermissionKey.LEADS_ASSIGN.value not in context.permissions:
            raise LeadPermissionError
        _validate_assignee(database_session, context, payload.assigned_user_id)
    if payload.status != LeadStatus.NEW:
        if PermissionKey.LEADS_STATUS.value not in context.permissions:
            raise LeadPermissionError
        _validate_transition(LeadStatus.NEW.value, payload.status)

    values = payload.model_dump(mode="python")
    values["email"] = normalized_email
    values["country"] = country_name_to_code(values.get("country"))
    lead = Lead(
        **values,
        email_original=str(payload.email),
        tenant_id=context.tenant.id,
        tracking_id=_tracking_id(),
        source=LeadSource.MANUAL.value,
        created_by=context.user.id,
        updated_by=context.user.id,
    )
    database_session.add(lead)
    try:
        database_session.flush()
    except IntegrityError as exception:
        database_session.rollback()
        existing = get_active_duplicate(database_session, context.tenant.id, normalized_email)
        if existing is not None:
            raise LeadDuplicateError(existing) from exception
        raise

    add_lead_event(
        database_session,
        lead=lead,
        actor_user_id=context.user.id,
        event_type="lead.created",
        details={"source": LeadSource.MANUAL.value},
    )
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.created",
        target_membership_id=context.membership.id,
        details={"lead_id": str(lead.id), "tracking_id": lead.tracking_id},
    )
    database_session.commit()
    database_session.refresh(lead)
    return lead


def get_lead_for_context(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
) -> Lead:
    lead = get_lead(database_session, context.tenant.id, lead_id)
    if lead is None:
        raise LeadNotFoundError
    _enforce_scope(context, lead, write=False)
    return lead


def get_lead_assignment_history(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    *,
    page: int,
    size: int,
) -> AssignmentHistoryPage:
    get_lead_for_context(database_session, context, lead_id)
    return list_assignment_history(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        page=page,
        size=size,
    )


def get_leads(
    database_session: Session,
    context: WorkspaceContext,
    *,
    page: int,
    size: int,
    filters: LeadFilterParams,
) -> Page:
    assigned_scope = context.user.id if _is_staff(context) else None
    return list_leads(
        database_session,
        tenant_id=context.tenant.id,
        page=page,
        size=size,
        filters=filters,
        assigned_scope_user_id=assigned_scope,
    )


def get_service_filter_options(
    database_session: Session,
    context: WorkspaceContext,
    *,
    page: int,
    size: int,
    search: str | None,
) -> Page:
    assigned_scope = context.user.id if _is_staff(context) else None
    return list_service_options(
        database_session,
        tenant_id=context.tenant.id,
        page=page,
        size=size,
        search=search,
        assigned_scope_user_id=assigned_scope,
    )


def _validated_combined_values(lead: Lead, changes: dict[str, Any]) -> dict[str, Any]:
    field_names = LeadFields.model_fields.keys()
    combined = {name: getattr(lead, name) for name in field_names}
    combined.update({key: value for key, value in changes.items() if key in field_names})
    validated = LeadFields.model_validate(combined)
    normalized = validated.model_dump(mode="python")
    return {key: normalized[key] for key in changes if key in normalized}


def _normalize_assignee_id(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value).strip())


def _apply_pending_task_disposition(
    database_session: Session,
    context: WorkspaceContext,
    lead: Lead,
    *,
    new_assignee_id: uuid.UUID | None,
    disposition: str,
    now: datetime,
) -> int:
    if disposition == "transfer" and new_assignee_id is None:
        raise LeadAssignmentDispositionError(
            "Pending tasks cannot be transferred when unassigning a lead"
        )
    tasks = lock_open_tasks_for_lead(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead.id,
    )
    if disposition == "retain":
        invalid_assignees = {
            task.assigned_user_id
            for task in tasks
            if not active_assignee_exists(
                database_session,
                context.tenant.id,
                task.assigned_user_id,
            )
        }
        if invalid_assignees:
            raise LeadAssignmentDispositionError(
                "Pending tasks can be retained only for active users in this workspace"
            )
        return len(tasks)

    for task in tasks:
        if disposition == "transfer":
            assert new_assignee_id is not None
            task.assigned_user_id = new_assignee_id
        elif disposition == "cancel":
            task.status = "cancelled"
            task.cancelled_by = context.user.id
            task.cancelled_at = now
            task.cancellation_reason = "Cancelled during lead reassignment"
        else:
            raise LeadAssignmentDispositionError(
                "pending_task_disposition must be transfer, retain, or cancel"
            )
        task.updated_by = context.user.id
        task.updated_at = now
        task.row_version += 1
    return len(tasks)


def update_lead(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    payload: LeadUpdateRequest,
) -> Lead:
    lead = get_lead(database_session, context.tenant.id, lead_id, for_update=True)
    if lead is None:
        raise LeadNotFoundError
    _enforce_scope(context, lead, write=True)

    changes = payload.model_dump(exclude_unset=True, mode="python")
    expected_version = int(changes.pop("row_version"))
    assignment_reason = changes.pop("assignment_reason", None)
    task_disposition = changes.pop("pending_task_disposition", None)
    if expected_version != lead.row_version:
        raise LeadConflictError

    assignee_field_supplied = "assigned_user_id" in changes
    persisted_assignee_id = _normalize_assignee_id(lead.assigned_user_id)
    requested_assignee_id = (
        _normalize_assignee_id(changes["assigned_user_id"])
        if assignee_field_supplied
        else persisted_assignee_id
    )
    assignment_changed = (
        assignee_field_supplied
        and requested_assignee_id != persisted_assignee_id
    )
    if assignment_changed:
        if PermissionKey.LEADS_ASSIGN.value not in context.permissions:
            raise LeadPermissionError
        _validate_assignee(database_session, context, requested_assignee_id)
        if not assignment_reason:
            raise LeadAssignmentDispositionError("assignment_reason is required")
        if task_disposition is None:
            raise LeadAssignmentDispositionError(
                "pending_task_disposition is required when changing assignee"
            )
        if requested_assignee_id is None and task_disposition == "transfer":
            raise LeadAssignmentDispositionError(
                "Pending tasks cannot be transferred when unassigning a lead"
            )
        changes["assigned_user_id"] = requested_assignee_id
    elif assignee_field_supplied:
        # An unchanged assignee is an idempotent no-op, even when form clients
        # submit the complete current record.
        changes.pop("assigned_user_id")
    if "status" in changes:
        if PermissionKey.LEADS_STATUS.value not in context.permissions:
            raise LeadPermissionError
        if changes["status"] == LeadStatus.CONVERTED:
            raise LeadExplicitConversionRequiredError
        _validate_transition(lead.status, LeadStatus(changes["status"]))

    if not changes:
        database_session.commit()
        database_session.refresh(lead)
        return lead

    try:
        validated_changes = _validated_combined_values(lead, changes)
    except ValidationError as exception:
        raise LeadTransitionError(str(exception)) from exception
    for field in ("status", "assigned_user_id"):
        if field in changes:
            validated_changes[field] = changes[field]
    if "country" in validated_changes:
        validated_changes["country"] = country_name_to_code(validated_changes["country"])
    if "email" in validated_changes:
        normalized_email = str(validated_changes["email"]).lower()
        duplicate = get_active_duplicate(
            database_session,
            context.tenant.id,
            normalized_email,
            exclude_lead_id=lead.id,
        )
        if duplicate is not None:
            raise LeadDuplicateError(duplicate)
        validated_changes["email"] = normalized_email
        validated_changes["email_original"] = normalized_email

    status_changed = "status" in validated_changes and validated_changes["status"] != lead.status
    previous_status = lead.status
    previous_assignee_id = persisted_assignee_id
    now = datetime.now(timezone.utc)
    validated_changes["updated_by"] = context.user.id
    validated_changes["updated_at"] = now
    try:
        affected_task_count = 0
        if assignment_changed:
            new_assignee_id = validated_changes["assigned_user_id"]
            affected_task_count = _apply_pending_task_disposition(
                database_session,
                context,
                lead,
                new_assignee_id=new_assignee_id,
                disposition=task_disposition,
                now=now,
            )
            # The session deliberately has autoflush disabled. Persist the locked task
            # changes before expiring objects to reload the versioned lead update.
            database_session.flush()
        updated = update_lead_versioned(
            database_session,
            lead,
            expected_version,
            validated_changes,
        )
        if not updated:
            database_session.rollback()
            raise LeadConflictError
        database_session.expire_all()
        updated_lead = get_lead(database_session, context.tenant.id, lead_id)
        if updated_lead is None:
            raise LeadNotFoundError
        changed_fields = sorted(key for key in validated_changes if key not in {"updated_by", "updated_at"})
        add_lead_event(
            database_session,
            lead=updated_lead,
            actor_user_id=context.user.id,
            event_type="lead.updated",
            details={"changed_fields": changed_fields},
        )
        if assignment_changed:
            add_assignment_history(
                database_session,
                tenant_id=context.tenant.id,
                lead_id=lead_id,
                previous_assignee_id=previous_assignee_id,
                new_assignee_id=updated_lead.assigned_user_id,
                actor_user_id=context.user.id,
                reason=assignment_reason,
                task_disposition=task_disposition,
                affected_task_count=affected_task_count,
                lead_row_version=expected_version + 1,
            )
            add_lead_event(
                database_session,
                lead=updated_lead,
                actor_user_id=context.user.id,
                event_type="lead.assigned",
                details={
                    "previous_assignee_id": str(previous_assignee_id) if previous_assignee_id else None,
                    "new_assignee_id": (
                        str(updated_lead.assigned_user_id)
                        if updated_lead.assigned_user_id
                        else None
                    ),
                    "reason": assignment_reason,
                    "pending_task_disposition": task_disposition,
                    "affected_task_count": affected_task_count,
                },
            )
        if status_changed:
            add_lead_event(
                database_session,
                lead=updated_lead,
                actor_user_id=context.user.id,
                event_type="lead.status_changed",
                details={"previous": previous_status, "current": updated_lead.status},
            )
        add_authorization_audit(
            database_session,
            tenant_id=context.tenant.id,
            actor_user_id=context.user.id,
            action="lead.updated",
            target_membership_id=context.membership.id,
            details={
                "lead_id": str(lead_id),
                "changed_fields": changed_fields,
                "assignment_task_disposition": task_disposition if assignment_changed else None,
            },
        )
        database_session.commit()
        database_session.refresh(updated_lead)
        return updated_lead
    except IntegrityError as exception:
        database_session.rollback()
        duplicate = get_active_duplicate(
            database_session,
            context.tenant.id,
            str(validated_changes.get("email", lead.email)),
            exclude_lead_id=lead.id,
        )
        if duplicate is not None:
            raise LeadDuplicateError(duplicate) from exception
        raise


def archive_lead(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    row_version: int,
) -> None:
    lead = get_lead(database_session, context.tenant.id, lead_id)
    if lead is None:
        raise LeadNotFoundError
    _enforce_scope(context, lead, write=True)
    if lead.row_version != row_version:
        raise LeadConflictError
    now = datetime.now(timezone.utc)
    if not update_lead_versioned(
        database_session,
        lead,
        row_version,
        {"deleted_at": now, "deleted_by": context.user.id, "updated_by": context.user.id, "updated_at": now},
    ):
        database_session.rollback()
        raise LeadConflictError
    cancelled = cancel_pending_customer_actions(
        database_session,
        lead.id,
        now,
        cancelled_by=context.user.id,
        cancellation_reason="Lead archived",
    )
    add_lead_event(
        database_session,
        lead=lead,
        actor_user_id=context.user.id,
        event_type="lead.archived",
        details={"cancelled_pending_actions": cancelled},
    )
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.archived",
        target_membership_id=context.membership.id,
        details={"lead_id": str(lead.id), "cancelled_pending_actions": cancelled},
    )
    database_session.commit()
