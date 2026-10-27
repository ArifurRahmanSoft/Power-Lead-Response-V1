import uuid
from dataclasses import dataclass

from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.orm import Session

from app.models.sender_account import SenderAccount


@dataclass(frozen=True)
class PaginatedSenderAccounts:
    items: list[SenderAccount]
    total: int


def list_sender_accounts(
    database_session: Session,
    tenant_id: uuid.UUID,
    *,
    offset: int,
    limit: int,
    include_inactive: bool,
) -> PaginatedSenderAccounts:
    condition = SenderAccount.tenant_id == tenant_id
    if not include_inactive:
        condition = condition & SenderAccount.is_active.is_(True)
    total = database_session.scalar(
        select(func.count(SenderAccount.id)).where(condition)
    ) or 0
    items = list(
        database_session.scalars(
            select(SenderAccount)
            .where(condition)
            .order_by(
                SenderAccount.is_default.desc(),
                SenderAccount.display_name,
                SenderAccount.id,
            )
            .offset(offset)
            .limit(limit)
        ).all()
    )
    return PaginatedSenderAccounts(items=items, total=total)


def get_sender_account(
    database_session: Session,
    tenant_id: uuid.UUID,
    sender_account_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> SenderAccount | None:
    statement = select(SenderAccount).where(
        SenderAccount.id == sender_account_id,
        SenderAccount.tenant_id == tenant_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def sender_email_exists(
    database_session: Session,
    tenant_id: uuid.UUID,
    sender_email: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    statement = select(SenderAccount.id).where(
        SenderAccount.tenant_id == tenant_id,
        SenderAccount.sender_email == sender_email,
    )
    if exclude_id is not None:
        statement = statement.where(SenderAccount.id != exclude_id)
    return database_session.scalar(statement) is not None


def clear_default_sender(
    database_session: Session,
    tenant_id: uuid.UUID,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    statement = (
        update(SenderAccount)
        .where(
            SenderAccount.tenant_id == tenant_id,
            SenderAccount.is_default.is_(True),
        )
        .values(is_default=False)
    )
    if exclude_id is not None:
        statement = statement.where(SenderAccount.id != exclude_id)
    database_session.execute(statement)


def sender_account_is_referenced_by_message_history(
    database_session: Session,
    sender_account_id: uuid.UUID,
) -> bool:
    """Support existing/future mail history schemas without inventing one here."""
    bind = database_session.get_bind()
    inspector = inspect(bind)
    known_history_tables = (
        "t_outbound_message",
        "t_outbound_message_batch",
        "t_mail_message",
        "t_email_message",
        "t_message",
    )
    available = set(inspector.get_table_names())
    for table_name in known_history_tables:
        if table_name not in available:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if "sender_account_id" not in columns:
            continue
        statement = text(
            f'SELECT 1 FROM "{table_name}" WHERE sender_account_id = :account_id LIMIT 1'
        )
        if database_session.execute(
            statement,
            {"account_id": sender_account_id},
        ).first() is not None:
            return True
    return False
