import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sms_message import (
    SmsBatch,
    SmsBatchRecipient,
    SmsMessage,
    SmsMessageEvent,
)


def get_batch_by_idempotency(
    database_session: Session, tenant_id: uuid.UUID, key: str
) -> SmsBatch | None:
    return database_session.scalar(
        select(SmsBatch).where(
            SmsBatch.tenant_id == tenant_id,
            SmsBatch.idempotency_key == key,
        )
    )


def get_sms_batch(
    database_session: Session, tenant_id: uuid.UUID, batch_id: uuid.UUID
) -> SmsBatch | None:
    return database_session.scalar(
        select(SmsBatch).where(
            SmsBatch.id == batch_id,
            SmsBatch.tenant_id == tenant_id,
        )
    )


def list_batch_recipients(
    database_session: Session, batch_id: uuid.UUID
) -> list[SmsBatchRecipient]:
    return list(
        database_session.scalars(
            select(SmsBatchRecipient)
            .where(SmsBatchRecipient.batch_id == batch_id)
            .order_by(SmsBatchRecipient.ordinal, SmsBatchRecipient.id)
        ).all()
    )


def list_batch_messages(
    database_session: Session, batch_id: uuid.UUID
) -> list[SmsMessage]:
    return list(
        database_session.scalars(
            select(SmsMessage)
            .where(SmsMessage.batch_id == batch_id)
            .order_by(SmsMessage.created_at, SmsMessage.id)
        ).all()
    )


def get_sms_message(
    database_session: Session,
    tenant_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> SmsMessage | None:
    statement = select(SmsMessage).where(
        SmsMessage.id == message_id,
        SmsMessage.tenant_id == tenant_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def list_message_events(
    database_session: Session, message_id: uuid.UUID
) -> list[SmsMessageEvent]:
    return list(
        database_session.scalars(
            select(SmsMessageEvent)
            .where(SmsMessageEvent.sms_message_id == message_id)
            .order_by(SmsMessageEvent.occurred_at, SmsMessageEvent.id)
        ).all()
    )


def add_message_event(
    database_session: Session,
    message: SmsMessage,
    status: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    provider_error_code: str | None = None,
    safe_reason: str | None = None,
    occurred_at=None,
) -> SmsMessageEvent:
    event = SmsMessageEvent(
        tenant_id=message.tenant_id,
        sms_message_id=message.id,
        status=status,
        actor_user_id=actor_user_id,
        provider_error_code=provider_error_code,
        safe_reason=safe_reason,
    )
    if occurred_at is not None:
        event.occurred_at = occurred_at
    database_session.add(event)
    return event
