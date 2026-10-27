import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.lead_activity import LeadActivity
from app.models.membership import TenantMembership
from app.models.user import User


@dataclass(frozen=True)
class LeadActivityPage:
    items: list[LeadActivity]
    total: int


def get_activity(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    activity_id: uuid.UUID,
) -> LeadActivity | None:
    return database_session.scalar(
        select(LeadActivity).where(
            LeadActivity.id == activity_id,
            LeadActivity.tenant_id == tenant_id,
            LeadActivity.lead_id == lead_id,
        )
    )


def list_activities(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    page: int,
    size: int,
) -> LeadActivityPage:
    conditions = (
        LeadActivity.tenant_id == tenant_id,
        LeadActivity.lead_id == lead_id,
    )
    total = database_session.scalar(
        select(func.count(LeadActivity.id)).where(*conditions)
    ) or 0
    items = list(
        database_session.scalars(
            select(LeadActivity)
            .where(*conditions)
            .order_by(
                LeadActivity.occurred_at.desc(),
                LeadActivity.recorded_at.desc(),
                LeadActivity.id.desc(),
            )
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    return LeadActivityPage(items=items, total=total)


def active_tenant_user_exists(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    return database_session.scalar(
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
    ) is not None
