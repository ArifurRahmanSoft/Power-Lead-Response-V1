import uuid
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.permissions import PermissionKey, RoleKey
from app.core.security import WorkspaceContext
from app.models.lead_task import LeadTask
from app.repositories.lead_activity_repository import get_activity
from app.repositories.lead_repository import active_assignee_exists
from app.repositories.lead_task_repository import (
    LeadTaskPage,
    get_task,
    list_tasks,
    update_task_versioned,
)
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.lead_activity import LeadActivityType
from app.schemas.lead_task import (
    CONTACT_TASK_TYPES,
    LeadTaskCompleteRequest,
    LeadTaskCreateRequest,
    LeadTaskListFilters,
    LeadTaskStatus,
    LeadTaskUpdateRequest,
)
from app.services.lead_activity_service import create_manual_lead_activity
from app.services.lead_service import get_lead_for_context


CONTACT_ACTIVITY_TYPES = frozenset(
    {
        LeadActivityType.INITIAL_CONTACT.value,
        LeadActivityType.FOLLOW_UP.value,
        LeadActivityType.INBOUND_REPLY.value,
    }
)


class LeadTaskNotFoundError(Exception):
    pass


class LeadTaskConflictError(Exception):
    pass


class LeadTaskStateError(Exception):
    pass


class LeadTaskAssignmentError(Exception):
    pass


class LeadTaskReferenceError(Exception):
    pass


class LeadTaskPermissionError(Exception):
    pass


def _is_staff(context: WorkspaceContext) -> bool:
    return context.role == RoleKey.STAFF.value


def _validate_assignment(
    database_session: Session,
    context: WorkspaceContext,
    assigned_user_id: uuid.UUID,
) -> None:
    if assigned_user_id != context.user.id and (
        PermissionKey.LEADS_ASSIGN.value not in context.permissions
    ):
        raise LeadTaskPermissionError
    if not active_assignee_exists(
        database_session,
        context.tenant.id,
        assigned_user_id,
    ):
        raise LeadTaskAssignmentError


def _validate_activity_reference(
    database_session: Session,
    context: WorkspaceContext,
    *,
    lead_id: uuid.UUID,
    activity_id: uuid.UUID,
    contact_required: bool,
) -> None:
    activity = get_activity(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        activity_id=activity_id,
    )
    if activity is None:
        raise LeadTaskReferenceError(
            "related_activity_id must reference an activity for this lead"
        )
    if contact_required and activity.activity_type not in CONTACT_ACTIVITY_TYPES:
        raise LeadTaskReferenceError(
            "A completed contact task must reference a contact activity"
        )


def _get_task_for_context(
    database_session: Session,
    context: WorkspaceContext,
    task_id: uuid.UUID,
    *,
    for_update: bool,
    write: bool,
) -> LeadTask:
    task = get_task(
        database_session,
        tenant_id=context.tenant.id,
        task_id=task_id,
        for_update=for_update,
    )
    if task is None:
        raise LeadTaskNotFoundError
    get_lead_for_context(database_session, context, task.lead_id)
    if write and _is_staff(context) and task.assigned_user_id != context.user.id:
        raise LeadTaskPermissionError
    return task


def create_lead_task(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    payload: LeadTaskCreateRequest,
) -> LeadTask:
    get_lead_for_context(database_session, context, lead_id)
    _validate_assignment(database_session, context, payload.assigned_user_id)
    if payload.related_activity_id is not None:
        _validate_activity_reference(
            database_session,
            context,
            lead_id=lead_id,
            activity_id=payload.related_activity_id,
            contact_required=payload.task_type in CONTACT_TASK_TYPES,
        )
    task = LeadTask(
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        **payload.model_dump(mode="python"),
        status=LeadTaskStatus.PENDING.value,
        created_by=context.user.id,
        updated_by=context.user.id,
    )
    database_session.add(task)
    database_session.flush()
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.task.created",
        target_membership_id=context.membership.id,
        details={
            "lead_id": str(lead_id),
            "task_id": str(task.id),
            "assigned_user_id": str(task.assigned_user_id),
        },
    )
    database_session.commit()
    database_session.refresh(task)
    return task


def get_lead_tasks(
    database_session: Session,
    context: WorkspaceContext,
    *,
    page: int,
    size: int,
    filters: LeadTaskListFilters,
) -> LeadTaskPage:
    if filters.lead_id is not None:
        get_lead_for_context(database_session, context, filters.lead_id)
    now_utc = datetime.now(timezone.utc)
    today_start_utc = None
    tomorrow_start_utc = None
    if filters.due_today:
        zone = ZoneInfo(filters.timezone_name)
        local_today = now_utc.astimezone(zone).date()
        today_start_utc = datetime.combine(local_today, time.min, zone).astimezone(
            timezone.utc
        )
        tomorrow_start_utc = datetime.combine(
            local_today.fromordinal(local_today.toordinal() + 1),
            time.min,
            zone,
        ).astimezone(timezone.utc)
    return list_tasks(
        database_session,
        tenant_id=context.tenant.id,
        current_user_id=context.user.id,
        assigned_scope_user_id=context.user.id if _is_staff(context) else None,
        page=page,
        size=size,
        filters=filters,
        today_start_utc=today_start_utc,
        tomorrow_start_utc=tomorrow_start_utc,
        now_utc=now_utc,
    )


def _updated_task(
    database_session: Session,
    context: WorkspaceContext,
    task_id: uuid.UUID,
) -> LeadTask:
    database_session.expire_all()
    task = get_task(
        database_session,
        tenant_id=context.tenant.id,
        task_id=task_id,
    )
    if task is None:
        raise LeadTaskNotFoundError
    return task


def update_lead_task(
    database_session: Session,
    context: WorkspaceContext,
    task_id: uuid.UUID,
    payload: LeadTaskUpdateRequest,
) -> LeadTask:
    task = _get_task_for_context(
        database_session, context, task_id, for_update=True, write=True
    )
    if task.row_version != payload.row_version:
        raise LeadTaskConflictError
    if task.status in {LeadTaskStatus.COMPLETED.value, LeadTaskStatus.CANCELLED.value}:
        raise LeadTaskStateError("Completed or cancelled tasks cannot be updated")

    changes = payload.model_dump(exclude_unset=True, mode="python")
    expected_version = changes.pop("row_version")
    if "assigned_user_id" in changes:
        _validate_assignment(database_session, context, changes["assigned_user_id"])
    resulting_type = changes.get("task_type", task.task_type)
    resulting_activity_id = changes.get("related_activity_id", task.related_activity_id)
    if resulting_activity_id is not None:
        _validate_activity_reference(
            database_session,
            context,
            lead_id=task.lead_id,
            activity_id=resulting_activity_id,
            contact_required=str(resulting_type) in {
                item.value for item in CONTACT_TASK_TYPES
            },
        )
    changes["updated_by"] = context.user.id
    changes["updated_at"] = datetime.now(timezone.utc)
    if not update_task_versioned(database_session, task, expected_version, changes):
        database_session.rollback()
        raise LeadTaskConflictError
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.task.updated",
        target_membership_id=context.membership.id,
        details={
            "lead_id": str(task.lead_id),
            "task_id": str(task.id),
            "changed_fields": sorted(
                key for key in changes if key not in {"updated_by", "updated_at"}
            ),
        },
    )
    database_session.commit()
    return _updated_task(database_session, context, task.id)


def cancel_lead_task(
    database_session: Session,
    context: WorkspaceContext,
    task_id: uuid.UUID,
    *,
    row_version: int,
) -> LeadTask:
    task = _get_task_for_context(
        database_session, context, task_id, for_update=True, write=True
    )
    if task.row_version != row_version:
        raise LeadTaskConflictError
    if task.status in {LeadTaskStatus.COMPLETED.value, LeadTaskStatus.CANCELLED.value}:
        raise LeadTaskStateError("Task is already completed or cancelled")
    now = datetime.now(timezone.utc)
    if not update_task_versioned(
        database_session,
        task,
        row_version,
        {
            "status": LeadTaskStatus.CANCELLED.value,
            "cancelled_by": context.user.id,
            "cancelled_at": now,
            "updated_by": context.user.id,
            "updated_at": now,
        },
    ):
        database_session.rollback()
        raise LeadTaskConflictError
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.task.cancelled",
        target_membership_id=context.membership.id,
        details={"lead_id": str(task.lead_id), "task_id": str(task.id)},
    )
    database_session.commit()
    return _updated_task(database_session, context, task.id)


def complete_lead_task(
    database_session: Session,
    context: WorkspaceContext,
    task_id: uuid.UUID,
    payload: LeadTaskCompleteRequest,
) -> LeadTask:
    task = _get_task_for_context(
        database_session, context, task_id, for_update=True, write=True
    )
    if task.row_version != payload.row_version:
        raise LeadTaskConflictError
    if task.status in {LeadTaskStatus.COMPLETED.value, LeadTaskStatus.CANCELLED.value}:
        raise LeadTaskStateError("Task is already completed or cancelled")

    is_contact_task = task.task_type in {item.value for item in CONTACT_TASK_TYPES}
    related_activity_id = payload.related_activity_id or task.related_activity_id
    if payload.activity is not None:
        if (
            is_contact_task
            and payload.activity.activity_type.value not in CONTACT_ACTIVITY_TYPES
        ):
            raise LeadTaskReferenceError(
                "A completed contact task must create a contact activity"
            )
        activity = create_manual_lead_activity(
            database_session,
            context,
            task.lead_id,
            payload.activity,
            commit=False,
        )
        related_activity_id = activity.id

    if is_contact_task and related_activity_id is None:
        raise LeadTaskReferenceError(
            "A completed contact task must reference a contact activity"
        )
    if related_activity_id is not None:
        _validate_activity_reference(
            database_session,
            context,
            lead_id=task.lead_id,
            activity_id=related_activity_id,
            contact_required=is_contact_task,
        )

    now = datetime.now(timezone.utc)
    if not update_task_versioned(
        database_session,
        task,
        payload.row_version,
        {
            "status": LeadTaskStatus.COMPLETED.value,
            "related_activity_id": related_activity_id,
            "completed_by": context.user.id,
            "completed_at": now,
            "updated_by": context.user.id,
            "updated_at": now,
        },
    ):
        database_session.rollback()
        raise LeadTaskConflictError
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.task.completed",
        target_membership_id=context.membership.id,
        details={
            "lead_id": str(task.lead_id),
            "task_id": str(task.id),
            "related_activity_id": (
                str(related_activity_id) if related_activity_id else None
            ),
        },
    )
    database_session.commit()
    return _updated_task(database_session, context, task.id)
