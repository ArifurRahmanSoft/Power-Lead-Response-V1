import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.lead_booking import (
    LeadAutomationStopEvent,
    LeadBooking,
    LeadBookingEvent,
)


@dataclass(frozen=True)
class EngagementPage:
    items: list[Any]
    total: int


def get_booking(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    for_update: bool = False,
) -> LeadBooking | None:
    statement = select(LeadBooking).where(
        LeadBooking.id == booking_id,
        LeadBooking.tenant_id == tenant_id,
        LeadBooking.lead_id == lead_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def list_bookings(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    page: int,
    size: int,
) -> EngagementPage:
    conditions = (
        LeadBooking.tenant_id == tenant_id,
        LeadBooking.lead_id == lead_id,
    )
    total = database_session.scalar(
        select(func.count(LeadBooking.id)).where(*conditions)
    ) or 0
    items = list(
        database_session.scalars(
            select(LeadBooking)
            .where(*conditions)
            .order_by(LeadBooking.scheduled_at.desc(), LeadBooking.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    return EngagementPage(items=items, total=total)


def list_booking_history(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    booking_id: uuid.UUID,
    page: int,
    size: int,
) -> EngagementPage:
    conditions = (
        LeadBookingEvent.tenant_id == tenant_id,
        LeadBookingEvent.booking_id == booking_id,
    )
    total = database_session.scalar(
        select(func.count(LeadBookingEvent.id)).where(*conditions)
    ) or 0
    items = list(
        database_session.scalars(
            select(LeadBookingEvent)
            .where(*conditions)
            .order_by(LeadBookingEvent.recorded_at.desc(), LeadBookingEvent.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    return EngagementPage(items=items, total=total)


def add_booking_event(
    database_session: Session,
    *,
    booking: LeadBooking,
    event_type: str,
    previous_status: str | None,
    new_status: str,
    previous_scheduled_at: datetime | None,
    new_scheduled_at: datetime,
    previous_timezone: str | None,
    new_timezone: str,
    notes: str | None,
    recorded_by: uuid.UUID,
) -> LeadBookingEvent:
    event = LeadBookingEvent(
        tenant_id=booking.tenant_id,
        lead_id=booking.lead_id,
        booking_id=booking.id,
        event_type=event_type,
        previous_status=previous_status,
        new_status=new_status,
        previous_scheduled_at=previous_scheduled_at,
        new_scheduled_at=new_scheduled_at,
        previous_timezone=previous_timezone,
        new_timezone=new_timezone,
        notes=notes,
        recorded_by=recorded_by,
    )
    database_session.add(event)
    return event


def add_automation_stop_event(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    stop_key: str,
    trigger_type: str,
    trigger_id: uuid.UUID,
    recorded_by: uuid.UUID,
) -> LeadAutomationStopEvent:
    event = LeadAutomationStopEvent(
        tenant_id=tenant_id,
        lead_id=lead_id,
        stop_key=stop_key,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        recorded_by=recorded_by,
    )
    database_session.add(event)
    return event


def add_recovery_stop_event(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    trigger_type: str,
    trigger_id: uuid.UUID,
    recorded_by: uuid.UUID,
) -> LeadAutomationStopEvent:
    return add_automation_stop_event(
        database_session,
        tenant_id=tenant_id,
        lead_id=lead_id,
        stop_key="recovery_follow_up",
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        recorded_by=recorded_by,
    )


def recovery_follow_up_is_stopped(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
) -> bool:
    return database_session.scalar(
        select(LeadAutomationStopEvent.id)
        .where(
            LeadAutomationStopEvent.tenant_id == tenant_id,
            LeadAutomationStopEvent.lead_id == lead_id,
            LeadAutomationStopEvent.stop_key == "recovery_follow_up",
        )
        .limit(1)
    ) is not None


def update_booking_versioned(
    database_session: Session,
    booking: LeadBooking,
    expected_version: int,
    values: dict[str, Any],
) -> bool:
    result = database_session.execute(
        update(LeadBooking)
        .where(
            LeadBooking.id == booking.id,
            LeadBooking.tenant_id == booking.tenant_id,
            LeadBooking.row_version == expected_version,
        )
        .values(**values, row_version=LeadBooking.row_version + 1)
        .returning(LeadBooking.id)
    )
    return result.scalar_one_or_none() is not None
