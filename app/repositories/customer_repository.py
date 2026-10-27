import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead


@dataclass(frozen=True)
class CustomerPage:
    items: list[Customer]
    total: int


def get_customer(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    assigned_scope_user_id: uuid.UUID | None = None,
    for_update: bool = False,
) -> Customer | None:
    statement = select(Customer).where(
        Customer.tenant_id == tenant_id,
        Customer.id == customer_id,
    )
    if assigned_scope_user_id is not None:
        statement = (
            statement.join(
                LeadConversion,
                LeadConversion.customer_id == Customer.id,
            )
            .join(Lead, Lead.id == LeadConversion.lead_id)
            .where(
                Lead.tenant_id == tenant_id,
                Lead.assigned_user_id == assigned_scope_user_id,
            )
        )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement.limit(1))


def get_customer_by_email(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    email: str,
    for_update: bool = False,
) -> Customer | None:
    statement = select(Customer).where(
        Customer.tenant_id == tenant_id,
        Customer.email == email,
    )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def get_conversion_by_lead(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
) -> LeadConversion | None:
    return database_session.scalar(
        select(LeadConversion).where(
            LeadConversion.tenant_id == tenant_id,
            LeadConversion.lead_id == lead_id,
        )
    )


def list_customers(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    page: int,
    size: int,
    search: str | None,
    assigned_scope_user_id: uuid.UUID | None,
) -> CustomerPage:
    conditions = [Customer.tenant_id == tenant_id]
    if search:
        pattern = f"%{search.strip().lower()}%"
        conditions.append(
            or_(
                func.lower(Customer.first_name).like(pattern),
                func.lower(Customer.last_name).like(pattern),
                func.lower(Customer.email).like(pattern),
                func.lower(func.coalesce(Customer.company, "")).like(pattern),
            )
        )
    statement = select(Customer).where(*conditions)
    if assigned_scope_user_id is not None:
        statement = (
            statement.join(LeadConversion, LeadConversion.customer_id == Customer.id)
            .join(Lead, Lead.id == LeadConversion.lead_id)
            .where(
                Lead.tenant_id == tenant_id,
                Lead.assigned_user_id == assigned_scope_user_id,
            )
            .distinct()
        )
    total = database_session.scalar(
        select(func.count()).select_from(statement.subquery())
    ) or 0
    items = list(
        database_session.scalars(
            statement.order_by(
                Customer.last_name.asc(),
                Customer.first_name.asc(),
                Customer.id.asc(),
            )
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    return CustomerPage(items=items, total=total)


def list_customer_conversions(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    assigned_scope_user_id: uuid.UUID | None = None,
) -> list[LeadConversion]:
    statement = select(LeadConversion).where(
        LeadConversion.tenant_id == tenant_id,
        LeadConversion.customer_id == customer_id,
    )
    if assigned_scope_user_id is not None:
        statement = statement.join(Lead, Lead.id == LeadConversion.lead_id).where(
            Lead.tenant_id == tenant_id,
            Lead.assigned_user_id == assigned_scope_user_id,
        )
    return list(
        database_session.scalars(
            statement.order_by(
                LeadConversion.converted_at.desc(),
                LeadConversion.id.desc(),
            )
        ).all()
    )
