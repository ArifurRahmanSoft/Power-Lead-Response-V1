import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.message_template import MessageTemplate


@dataclass(frozen=True)
class MessageTemplatePage:
    items: list[MessageTemplate]
    total: int


def normalize_message_template_name(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def message_template_name_exists(
    database_session: Session,
    tenant_id: uuid.UUID,
    normalized_name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    statement = select(MessageTemplate.id).where(
        MessageTemplate.tenant_id == tenant_id,
        MessageTemplate.normalized_name == normalized_name,
        MessageTemplate.archived_at.is_(None),
    )
    if exclude_id is not None:
        statement = statement.where(MessageTemplate.id != exclude_id)
    return database_session.scalar(statement) is not None


def list_message_templates(
    database_session: Session,
    tenant_id: uuid.UUID,
    *,
    page: int,
    size: int,
    search: str | None,
    include_archived: bool,
) -> MessageTemplatePage:
    conditions = [MessageTemplate.tenant_id == tenant_id]
    if not include_archived:
        conditions.append(MessageTemplate.archived_at.is_(None))
    if search and search.strip():
        term = f"%{search.strip()}%"
        conditions.append(
            or_(MessageTemplate.name.ilike(term), MessageTemplate.body.ilike(term))
        )
    statement = select(MessageTemplate).where(*conditions)
    total = database_session.scalar(
        select(func.count()).select_from(statement.subquery())
    ) or 0
    items = list(
        database_session.scalars(
            statement.order_by(
                MessageTemplate.updated_at.desc(), MessageTemplate.id
            )
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    return MessageTemplatePage(items=items, total=total)


def get_message_template(
    database_session: Session,
    tenant_id: uuid.UUID,
    template_id: uuid.UUID,
    *,
    include_archived: bool = False,
    for_update: bool = False,
) -> MessageTemplate | None:
    statement = select(MessageTemplate).where(
        MessageTemplate.id == template_id,
        MessageTemplate.tenant_id == tenant_id,
    )
    if not include_archived:
        statement = statement.where(MessageTemplate.archived_at.is_(None))
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)
