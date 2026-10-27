import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.lead import Lead
from app.models.lead_task import LeadTask
from app.schemas.lead_task import LeadTaskListFilters, LeadTaskStatus


@dataclass(frozen=True)
class LeadTaskPage:
    items: list[LeadTask]
    total: int


def get_task(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    for_update: bool = False,
) -> LeadTask | None:
    statement = select(LeadTask).where(
        LeadTask.tenant_id == tenant_id,
        LeadTask.id == task_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def lock_open_tasks_for_lead(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
) -> list[LeadTask]:
    return list(
        database_session.scalars(
            select(LeadTask)
            .where(
                LeadTask.tenant_id == tenant_id,
                LeadTask.lead_id == lead_id,
                LeadTask.status.in_(
                    [LeadTaskStatus.PENDING.value, LeadTaskStatus.IN_PROGRESS.value]
                ),
            )
            .order_by(LeadTask.id)
            .with_for_update()
        ).all()
    )


def list_tasks(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    current_user_id: uuid.UUID,
    assigned_scope_user_id: uuid.UUID | None,
    page: int,
    size: int,
    filters: LeadTaskListFilters,
    today_start_utc: datetime | None,
    tomorrow_start_utc: datetime | None,
    now_utc: datetime,
) -> LeadTaskPage:
    conditions = [
        LeadTask.tenant_id == tenant_id,
        Lead.tenant_id == tenant_id,
        Lead.deleted_at.is_(None),
    ]
    if assigned_scope_user_id is not None:
        conditions.append(Lead.assigned_user_id == assigned_scope_user_id)
    if filters.lead_id is not None:
        conditions.append(LeadTask.lead_id == filters.lead_id)
    if filters.assigned_to_me:
        conditions.append(LeadTask.assigned_user_id == current_user_id)
    if filters.status is not None:
        conditions.append(LeadTask.status == filters.status.value)
    if filters.task_type is not None:
        conditions.append(LeadTask.task_type == filters.task_type.value)
    if filters.priority is not None:
        conditions.append(LeadTask.priority == filters.priority.value)
    if filters.due_today:
        conditions.extend(
            [LeadTask.due_at >= today_start_utc, LeadTask.due_at < tomorrow_start_utc]
        )
    if filters.overdue:
        conditions.extend(
            [
                LeadTask.due_at < now_utc,
                LeadTask.status.in_(
                    [LeadTaskStatus.PENDING.value, LeadTaskStatus.IN_PROGRESS.value]
                ),
            ]
        )

    base = select(LeadTask).join(Lead, Lead.id == LeadTask.lead_id).where(*conditions)
    total = database_session.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0
    items = list(
        database_session.scalars(
            base.order_by(LeadTask.due_at.asc(), LeadTask.id.asc())
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    return LeadTaskPage(items=items, total=total)


def update_task_versioned(
    database_session: Session,
    task: LeadTask,
    expected_version: int,
    values: dict,
) -> bool:
    result = database_session.execute(
        update(LeadTask)
        .where(
            LeadTask.id == task.id,
            LeadTask.tenant_id == task.tenant_id,
            LeadTask.row_version == expected_version,
        )
        .values(**values, row_version=LeadTask.row_version + 1)
        .returning(LeadTask.id)
    )
    return result.scalar_one_or_none() is not None
