import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.lead_assignment import LeadAssignmentHistory


@dataclass(frozen=True)
class AssignmentHistoryPage:
    items: list[LeadAssignmentHistory]
    total: int


def add_assignment_history(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    previous_assignee_id: uuid.UUID | None,
    new_assignee_id: uuid.UUID | None,
    actor_user_id: uuid.UUID,
    reason: str,
    task_disposition: str,
    affected_task_count: int,
    lead_row_version: int,
) -> LeadAssignmentHistory:
    history = LeadAssignmentHistory(
        tenant_id=tenant_id,
        lead_id=lead_id,
        previous_assignee_id=previous_assignee_id,
        new_assignee_id=new_assignee_id,
        actor_user_id=actor_user_id,
        reason=reason,
        task_disposition=task_disposition,
        affected_task_count=affected_task_count,
        lead_row_version=lead_row_version,
    )
    database_session.add(history)
    return history


def list_assignment_history(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    page: int,
    size: int,
) -> AssignmentHistoryPage:
    conditions = (
        LeadAssignmentHistory.tenant_id == tenant_id,
        LeadAssignmentHistory.lead_id == lead_id,
    )
    total = database_session.scalar(
        select(func.count(LeadAssignmentHistory.id)).where(*conditions)
    ) or 0
    items = list(
        database_session.scalars(
            select(LeadAssignmentHistory)
            .where(*conditions)
            .order_by(
                LeadAssignmentHistory.lead_row_version.desc(),
                LeadAssignmentHistory.assigned_at.desc(),
                LeadAssignmentHistory.id.desc(),
            )
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    return AssignmentHistoryPage(items=items, total=total)
