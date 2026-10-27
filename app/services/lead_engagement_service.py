import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.permissions import PermissionKey, RoleKey
from app.core.security import WorkspaceContext
from app.models.lead_activity import LeadActivity
from app.models.lead_booking import LeadBooking
from app.repositories.lead_activity_repository import get_activity
from app.repositories.lead_engagement_repository import (
    EngagementPage,
    add_booking_event,
    add_recovery_stop_event,
    get_booking,
    list_booking_history,
    list_bookings,
    update_booking_versioned,
)
from app.repositories.lead_repository import active_assignee_exists
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.lead_activity import (
    LeadActivityCreateRequest,
    LeadActivityDirection,
    LeadActivityType,
)
from app.schemas.lead_engagement import (
    BookingCreateRequest,
    BookingRescheduleRequest,
    BookingStatus,
    BookingTransitionRequest,
    ManualReplyCreateRequest,
)
from app.services.lead_activity_service import create_manual_lead_activity
from app.services.lead_service import get_lead_for_context


class LeadEngagementNotFoundError(Exception):
    pass


class LeadEngagementConflictError(Exception):
    pass


class LeadEngagementStateError(Exception):
    pass


class LeadEngagementReferenceError(Exception):
    pass


class LeadEngagementAssignmentError(Exception):
    pass


class LeadEngagementPermissionError(Exception):
    pass


def _is_staff(context: WorkspaceContext) -> bool:
    return context.role == RoleKey.STAFF.value


def _validate_responsible_user(
    database_session: Session,
    context: WorkspaceContext,
    user_id: uuid.UUID,
) -> None:
    if user_id != context.user.id and PermissionKey.LEADS_ASSIGN.value not in context.permissions:
        raise LeadEngagementPermissionError
    if not active_assignee_exists(database_session, context.tenant.id, user_id):
        raise LeadEngagementAssignmentError


def _validate_outbound_activity(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    activity_id: uuid.UUID,
) -> LeadActivity:
    activity = get_activity(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        activity_id=activity_id,
    )
    if activity is None or activity.direction != LeadActivityDirection.OUTBOUND.value:
        raise LeadEngagementReferenceError(
            "related_outbound_activity_id must reference an outbound activity for this lead"
        )
    return activity


def record_manual_reply(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    payload: ManualReplyCreateRequest,
) -> LeadActivity:
    get_lead_for_context(database_session, context, lead_id)
    _validate_outbound_activity(
        database_session,
        context,
        lead_id,
        payload.related_outbound_activity_id,
    )
    activity = create_manual_lead_activity(
        database_session,
        context,
        lead_id,
        LeadActivityCreateRequest(
            activity_type=LeadActivityType.INBOUND_REPLY,
            channel=payload.channel,
            direction=LeadActivityDirection.INBOUND,
            occurred_at=payload.occurred_at,
            performed_by=None,
            outcome="Manually recorded reply",
            notes=payload.summary,
            related_activity_id=payload.related_outbound_activity_id,
        ),
        commit=False,
    )
    activity.externally_performed = True
    activity.provider_verified = False
    add_recovery_stop_event(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        trigger_type="manual_reply",
        trigger_id=activity.id,
        recorded_by=context.user.id,
    )
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.reply.recorded",
        target_membership_id=context.membership.id,
        details={"lead_id": str(lead_id), "activity_id": str(activity.id)},
    )
    database_session.commit()
    database_session.refresh(activity)
    return activity


def _get_booking_for_context(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    *,
    for_update: bool,
    write: bool,
) -> LeadBooking:
    get_lead_for_context(database_session, context, lead_id)
    booking = get_booking(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        booking_id=booking_id,
        for_update=for_update,
    )
    if booking is None:
        raise LeadEngagementNotFoundError
    if write and _is_staff(context) and booking.responsible_user_id != context.user.id:
        raise LeadEngagementPermissionError
    return booking


def create_booking(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    payload: BookingCreateRequest,
) -> LeadBooking:
    get_lead_for_context(database_session, context, lead_id)
    _validate_responsible_user(database_session, context, payload.responsible_user_id)
    booking = LeadBooking(
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        **payload.model_dump(mode="python"),
        provider_verified=False,
        created_by=context.user.id,
        updated_by=context.user.id,
    )
    database_session.add(booking)
    database_session.flush()
    add_booking_event(
        database_session,
        booking=booking,
        event_type="created",
        previous_status=None,
        new_status=booking.status,
        previous_scheduled_at=None,
        new_scheduled_at=booking.scheduled_at,
        previous_timezone=None,
        new_timezone=booking.timezone,
        notes=booking.notes,
        recorded_by=context.user.id,
    )
    add_recovery_stop_event(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        trigger_type="booking_created",
        trigger_id=booking.id,
        recorded_by=context.user.id,
    )
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.booking.created",
        target_membership_id=context.membership.id,
        details={"lead_id": str(lead_id), "booking_id": str(booking.id)},
    )
    database_session.commit()
    database_session.refresh(booking)
    return booking


def get_bookings(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    *,
    page: int,
    size: int,
) -> EngagementPage:
    get_lead_for_context(database_session, context, lead_id)
    return list_bookings(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        page=page,
        size=size,
    )


def get_booking_history(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    *,
    page: int,
    size: int,
) -> EngagementPage:
    _get_booking_for_context(
        database_session, context, lead_id, booking_id, for_update=False, write=False
    )
    return list_booking_history(
        database_session,
        tenant_id=context.tenant.id,
        booking_id=booking_id,
        page=page,
        size=size,
    )


def _reload_booking(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
) -> LeadBooking:
    database_session.expire_all()
    booking = get_booking(
        database_session,
        tenant_id=context.tenant.id,
        lead_id=lead_id,
        booking_id=booking_id,
    )
    if booking is None:
        raise LeadEngagementNotFoundError
    return booking


def _change_booking(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    *,
    expected_version: int,
    event_type: str,
    new_status: str,
    new_scheduled_at: datetime,
    new_timezone: str,
    notes: str | None,
    stop_trigger_type: str | None,
) -> LeadBooking:
    booking = _get_booking_for_context(
        database_session, context, lead_id, booking_id, for_update=True, write=True
    )
    if booking.row_version != expected_version:
        raise LeadEngagementConflictError
    if booking.status in {BookingStatus.CANCELLED.value, BookingStatus.COMPLETED.value}:
        raise LeadEngagementStateError("Cancelled or completed bookings cannot be changed")
    if event_type == "confirmed" and booking.status != BookingStatus.SCHEDULED.value:
        raise LeadEngagementStateError("Only scheduled bookings can be confirmed")

    previous_status = booking.status
    previous_scheduled_at = booking.scheduled_at
    previous_timezone = booking.timezone
    now = datetime.now(timezone.utc)
    values = {
        "status": new_status,
        "scheduled_at": new_scheduled_at,
        "timezone": new_timezone,
        "updated_by": context.user.id,
        "updated_at": now,
    }
    if notes is not None:
        values["notes"] = notes
    if not update_booking_versioned(database_session, booking, expected_version, values):
        database_session.rollback()
        raise LeadEngagementConflictError
    add_booking_event(
        database_session,
        booking=booking,
        event_type=event_type,
        previous_status=previous_status,
        new_status=new_status,
        previous_scheduled_at=previous_scheduled_at,
        new_scheduled_at=new_scheduled_at,
        previous_timezone=previous_timezone,
        new_timezone=new_timezone,
        notes=notes,
        recorded_by=context.user.id,
    )
    if stop_trigger_type is not None:
        add_recovery_stop_event(
            database_session,
            tenant_id=context.tenant.id,
            lead_id=lead_id,
            trigger_type=stop_trigger_type,
            trigger_id=booking.id,
            recorded_by=context.user.id,
        )
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action=f"lead.booking.{event_type}",
        target_membership_id=context.membership.id,
        details={"lead_id": str(lead_id), "booking_id": str(booking.id)},
    )
    database_session.commit()
    return _reload_booking(database_session, context, lead_id, booking.id)


def confirm_booking(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    payload: BookingTransitionRequest,
) -> LeadBooking:
    booking = _get_booking_for_context(
        database_session, context, lead_id, booking_id, for_update=False, write=True
    )
    return _change_booking(
        database_session,
        context,
        lead_id,
        booking_id,
        expected_version=payload.row_version,
        event_type="confirmed",
        new_status=BookingStatus.CONFIRMED.value,
        new_scheduled_at=booking.scheduled_at,
        new_timezone=booking.timezone,
        notes=payload.notes,
        stop_trigger_type="booking_confirmed",
    )


def cancel_booking(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    payload: BookingTransitionRequest,
) -> LeadBooking:
    booking = _get_booking_for_context(
        database_session, context, lead_id, booking_id, for_update=False, write=True
    )
    return _change_booking(
        database_session,
        context,
        lead_id,
        booking_id,
        expected_version=payload.row_version,
        event_type="cancelled",
        new_status=BookingStatus.CANCELLED.value,
        new_scheduled_at=booking.scheduled_at,
        new_timezone=booking.timezone,
        notes=payload.notes,
        stop_trigger_type=None,
    )


def reschedule_booking(
    database_session: Session,
    context: WorkspaceContext,
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    payload: BookingRescheduleRequest,
) -> LeadBooking:
    booking = _get_booking_for_context(
        database_session, context, lead_id, booking_id, for_update=False, write=True
    )
    return _change_booking(
        database_session,
        context,
        lead_id,
        booking_id,
        expected_version=payload.row_version,
        event_type="rescheduled",
        new_status=booking.status,
        new_scheduled_at=payload.scheduled_at,
        new_timezone=payload.timezone,
        notes=payload.notes,
        stop_trigger_type="booking_rescheduled",
    )
