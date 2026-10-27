import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.customer import Customer, LeadConversion
from app.models.lead import Lead
from app.models.whatsapp import (
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppConversationRead,
    WhatsAppDeliveryEvent,
    WhatsAppMessage,
)


@dataclass(frozen=True)
class ConversationListItem:
    conversation: WhatsAppConversation
    lead_name: str | None
    customer_name: str | None
    channel_display_name: str | None
    unread_count: int


@dataclass(frozen=True)
class ConversationPage:
    items: list[ConversationListItem]
    total: int


@dataclass(frozen=True)
class MessagePage:
    items: list[WhatsAppMessage]
    total: int


def get_active_channel_by_provider_identity(
    database_session: Session,
    *,
    account_sid: str,
    sender_address: str,
) -> WhatsAppChannel | None:
    return database_session.scalar(
        select(WhatsAppChannel).where(
            WhatsAppChannel.provider == "twilio",
            WhatsAppChannel.twilio_account_sid == account_sid,
            WhatsAppChannel.sender_address == sender_address,
            WhatsAppChannel.is_active.is_(True),
        )
    )


def get_active_meta_channel_by_identity(
    database_session: Session,
    *,
    waba_id: str,
    phone_number_id: str,
) -> WhatsAppChannel | None:
    return database_session.scalar(
        select(WhatsAppChannel).where(
            WhatsAppChannel.provider == "meta",
            WhatsAppChannel.provider_account_id == waba_id,
            WhatsAppChannel.provider_channel_id == phone_number_id,
            WhatsAppChannel.is_active.is_(True),
        )
    )


def get_active_messenger_channel_by_page_id(
    database_session: Session,
    *,
    page_id: str,
) -> WhatsAppChannel | None:
    return database_session.scalar(
        select(WhatsAppChannel).where(
            WhatsAppChannel.provider == "messenger",
            WhatsAppChannel.provider_account_id == page_id,
            WhatsAppChannel.provider_channel_id == page_id,
            WhatsAppChannel.is_active.is_(True),
        )
    )


def get_channel_for_tenant(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    provider: str = "twilio",
) -> WhatsAppChannel | None:
    return database_session.scalar(
        select(WhatsAppChannel).where(
            WhatsAppChannel.tenant_id == tenant_id,
            WhatsAppChannel.provider == provider,
            WhatsAppChannel.is_active.is_(True),
        )
    )


def get_conversation_by_contact(
    database_session: Session,
    *,
    channel_id: uuid.UUID,
    external_address: str,
    for_update: bool = False,
) -> WhatsAppConversation | None:
    statement = select(WhatsAppConversation).where(
        WhatsAppConversation.channel_id == channel_id,
        WhatsAppConversation.external_address == external_address,
    )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def get_message_by_provider_sid(
    database_session: Session,
    provider_message_sid: str,
    *,
    provider: str | None = None,
) -> WhatsAppMessage | None:
    conditions: list[object] = [
        WhatsAppMessage.provider_message_sid == provider_message_sid
    ]
    if provider is not None:
        conditions.append(WhatsAppMessage.provider == provider)
    return database_session.scalar(select(WhatsAppMessage).where(*conditions))


def get_message_by_idempotency(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    idempotency_key: str,
    provider: str | None = None,
) -> WhatsAppMessage | None:
    conditions: list[object] = [
        WhatsAppMessage.tenant_id == tenant_id,
        WhatsAppMessage.idempotency_key == idempotency_key,
    ]
    if provider is not None:
        conditions.append(WhatsAppMessage.provider == provider)
    return database_session.scalar(select(WhatsAppMessage).where(*conditions))


def _staff_visibility(user_id: uuid.UUID) -> object:
    linked_assigned_customer = exists(
        select(LeadConversion.id)
        .join(Lead, Lead.id == LeadConversion.lead_id)
        .where(
            LeadConversion.customer_id == WhatsAppConversation.customer_id,
            LeadConversion.tenant_id == WhatsAppConversation.tenant_id,
            Lead.tenant_id == WhatsAppConversation.tenant_id,
            Lead.assigned_user_id == user_id,
        )
    )
    return or_(
        exists(
            select(Lead.id).where(
                Lead.id == WhatsAppConversation.lead_id,
                Lead.tenant_id == WhatsAppConversation.tenant_id,
                Lead.assigned_user_id == user_id,
            )
        ),
        linked_assigned_customer,
    )


def get_conversation_for_context(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    assigned_scope_user_id: uuid.UUID | None,
    provider: str | None = None,
    for_update: bool = False,
) -> WhatsAppConversation | None:
    statement = select(WhatsAppConversation).join(
        WhatsAppChannel, WhatsAppChannel.id == WhatsAppConversation.channel_id
    ).where(
        WhatsAppConversation.id == conversation_id,
        WhatsAppConversation.tenant_id == tenant_id,
    )
    if provider is not None:
        statement = statement.where(WhatsAppChannel.provider == provider)
    if assigned_scope_user_id is not None:
        statement = statement.where(_staff_visibility(assigned_scope_user_id))
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def list_conversations(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    current_user_id: uuid.UUID,
    assigned_scope_user_id: uuid.UUID | None,
    search: str | None,
    page: int,
    size: int,
    provider: str | None = None,
) -> ConversationPage:
    conditions: list[object] = [WhatsAppConversation.tenant_id == tenant_id]
    if provider is not None:
        conditions.append(WhatsAppChannel.provider == provider)
    if assigned_scope_user_id is not None:
        conditions.append(_staff_visibility(assigned_scope_user_id))
    normalized_search = " ".join((search or "").strip().split())
    if normalized_search:
        pattern = f"%{normalized_search.casefold()}%"
        conditions.append(
            or_(
                func.lower(WhatsAppConversation.external_address).like(pattern),
                func.lower(func.coalesce(WhatsAppConversation.provider_display_name, "")).like(pattern),
                func.lower(func.coalesce(WhatsAppConversation.last_message_preview, "")).like(pattern),
                func.lower(func.coalesce(Lead.first_name, "") + " " + func.coalesce(Lead.last_name, "")).like(pattern),
                func.lower(func.coalesce(Customer.first_name, "") + " " + func.coalesce(Customer.last_name, "")).like(pattern),
            )
        )

    read_at = (
        select(WhatsAppConversationRead.last_read_at)
        .where(
            WhatsAppConversationRead.conversation_id == WhatsAppConversation.id,
            WhatsAppConversationRead.user_id == current_user_id,
        )
        .correlate(WhatsAppConversation)
        .scalar_subquery()
    )
    unread_count = (
        select(func.count(WhatsAppMessage.id))
        .where(
            WhatsAppMessage.conversation_id == WhatsAppConversation.id,
            WhatsAppMessage.direction == "inbound",
            or_(read_at.is_(None), WhatsAppMessage.occurred_at > read_at),
        )
        .correlate(WhatsAppConversation)
        .scalar_subquery()
    )
    total = database_session.scalar(
        select(func.count(WhatsAppConversation.id))
        .join(WhatsAppChannel, WhatsAppChannel.id == WhatsAppConversation.channel_id)
        .outerjoin(Lead, Lead.id == WhatsAppConversation.lead_id)
        .outerjoin(Customer, Customer.id == WhatsAppConversation.customer_id)
        .where(*conditions)
    ) or 0
    rows = database_session.execute(
        select(
            WhatsAppConversation,
            Lead.first_name,
            Lead.last_name,
            Customer.first_name,
            Customer.last_name,
            WhatsAppChannel.display_name,
            unread_count.label("unread_count"),
        )
        .join(WhatsAppChannel, WhatsAppChannel.id == WhatsAppConversation.channel_id)
        .outerjoin(Lead, Lead.id == WhatsAppConversation.lead_id)
        .outerjoin(Customer, Customer.id == WhatsAppConversation.customer_id)
        .where(*conditions)
        .order_by(
            WhatsAppConversation.last_message_at.desc(),
            WhatsAppConversation.id.desc(),
        )
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return ConversationPage(
        items=[
            ConversationListItem(
                conversation=row[0],
                lead_name=" ".join(part for part in (row[1], row[2]) if part) or None,
                customer_name=" ".join(part for part in (row[3], row[4]) if part) or None,
                channel_display_name=row[5],
                unread_count=int(row[6] or 0),
            )
            for row in rows
        ],
        total=int(total),
    )


def list_messages(
    database_session: Session,
    *,
    conversation_id: uuid.UUID,
    page: int,
    size: int,
) -> MessagePage:
    condition = WhatsAppMessage.conversation_id == conversation_id
    total = database_session.scalar(
        select(func.count(WhatsAppMessage.id)).where(condition)
    ) or 0
    items = list(
        database_session.scalars(
            select(WhatsAppMessage)
            .where(condition)
            .order_by(WhatsAppMessage.occurred_at.desc(), WhatsAppMessage.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    return MessagePage(items=items, total=int(total))


def mark_conversation_read(
    database_session: Session,
    *,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> WhatsAppConversationRead:
    latest = database_session.scalar(
        select(WhatsAppMessage)
        .where(
            WhatsAppMessage.conversation_id == conversation_id,
            WhatsAppMessage.direction == "inbound",
        )
        .order_by(WhatsAppMessage.occurred_at.desc(), WhatsAppMessage.id.desc())
        .limit(1)
    )
    cursor = database_session.get(
        WhatsAppConversationRead,
        {"conversation_id": conversation_id, "user_id": user_id},
    )
    read_at = latest.occurred_at if latest else datetime.now(timezone.utc)
    if cursor is None:
        cursor = WhatsAppConversationRead(
            conversation_id=conversation_id,
            user_id=user_id,
            last_read_message_id=latest.id if latest else None,
            last_read_at=read_at,
        )
        database_session.add(cursor)
    else:
        cursor.last_read_message_id = latest.id if latest else None
        cursor.last_read_at = read_at
    return cursor


def get_conversation_unread_count(
    database_session: Session,
    *,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    last_read_at = database_session.scalar(
        select(WhatsAppConversationRead.last_read_at).where(
            WhatsAppConversationRead.conversation_id == conversation_id,
            WhatsAppConversationRead.user_id == user_id,
        )
    )
    conditions: list[object] = [
        WhatsAppMessage.conversation_id == conversation_id,
        WhatsAppMessage.direction == "inbound",
    ]
    if last_read_at is not None:
        conditions.append(WhatsAppMessage.occurred_at > last_read_at)
    return int(
        database_session.scalar(
            select(func.count(WhatsAppMessage.id)).where(*conditions)
        )
        or 0
    )


def list_unmatched_delivery_events(
    database_session: Session,
    *,
    provider_message_sid: str,
    provider: str | None = None,
) -> list[WhatsAppDeliveryEvent]:
    conditions: list[object] = [
        WhatsAppDeliveryEvent.provider_message_sid == provider_message_sid,
        WhatsAppDeliveryEvent.message_id.is_(None),
    ]
    if provider is not None:
        conditions.append(WhatsAppDeliveryEvent.provider == provider)
    return list(
        database_session.scalars(
            select(WhatsAppDeliveryEvent)
            .where(*conditions)
            .order_by(WhatsAppDeliveryEvent.occurred_at, WhatsAppDeliveryEvent.id)
        ).all()
    )
