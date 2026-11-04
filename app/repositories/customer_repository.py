import uuid
from dataclasses import dataclass

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead


@dataclass(frozen=True)
class CustomerPage:
    items: list[Customer]
    total: int


def _customer_scope_condition(assigned_scope_user_id: uuid.UUID):
    converted_assigned = exists(
        select(LeadConversion.id)
        .join(Lead, Lead.id == LeadConversion.lead_id)
        .where(
            LeadConversion.customer_id == Customer.id,
            LeadConversion.tenant_id == Customer.tenant_id,
            Lead.tenant_id == Customer.tenant_id,
            Lead.assigned_user_id == assigned_scope_user_id,
        )
    )
    return or_(
        Customer.assigned_user_id == assigned_scope_user_id,
        converted_assigned,
    )


def attach_customer_origins(
    database_session: Session,
    customers: list[Customer],
    *,
    tenant_id: uuid.UUID,
    assigned_scope_user_id: uuid.UUID | None = None,
) -> None:
    if not customers:
        return
    for customer in customers:
        customer.source_lead_id = None
        customer.source_lead_label = None
        customer.converted_at = None

    conditions = [
        LeadConversion.tenant_id == tenant_id,
        LeadConversion.customer_id.in_([customer.id for customer in customers]),
        Lead.tenant_id == tenant_id,
    ]
    if assigned_scope_user_id is not None:
        conditions.append(Lead.assigned_user_id == assigned_scope_user_id)
    rows = database_session.execute(
        select(
            LeadConversion.customer_id,
            LeadConversion.lead_id,
            LeadConversion.converted_at,
            Lead.first_name,
            Lead.last_name,
        )
        .join(Lead, Lead.id == LeadConversion.lead_id)
        .where(*conditions)
        .order_by(
            LeadConversion.customer_id,
            LeadConversion.converted_at.asc(),
            LeadConversion.id.asc(),
        )
    ).all()
    by_id = {customer.id: customer for customer in customers}
    attached: set[uuid.UUID] = set()
    for row in rows:
        if row.customer_id in attached:
            continue
        customer = by_id.get(row.customer_id)
        if customer is None:
            continue
        customer.source_type = "lead"
        customer.source_lead_id = row.lead_id
        customer.source_lead_label = " ".join(
            part for part in (row.first_name, row.last_name) if part
        )
        customer.converted_at = row.converted_at
        attached.add(row.customer_id)


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
        statement = statement.where(
            _customer_scope_condition(assigned_scope_user_id)
        )
    if for_update:
        statement = statement.with_for_update()
    customer = database_session.scalar(statement.limit(1))
    if customer is not None:
        attach_customer_origins(
            database_session,
            [customer],
            tenant_id=tenant_id,
            assigned_scope_user_id=assigned_scope_user_id,
        )
    return customer


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
                func.lower(func.coalesce(Customer.designation, "")).like(pattern),
                func.lower(func.coalesce(Customer.service_requested, "")).like(pattern),
            )
        )
    statement = select(Customer).where(*conditions)
    if assigned_scope_user_id is not None:
        statement = statement.where(
            _customer_scope_condition(assigned_scope_user_id)
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
    attach_customer_origins(
        database_session,
        items,
        tenant_id=tenant_id,
        assigned_scope_user_id=assigned_scope_user_id,
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
