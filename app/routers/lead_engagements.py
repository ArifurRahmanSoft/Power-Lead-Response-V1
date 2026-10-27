import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.schemas.lead_engagement import (
    BookingCreateRequest,
    BookingData,
    BookingEventData,
    BookingHistoryData,
    BookingHistoryResponse,
    BookingListData,
    BookingListResponse,
    BookingRescheduleRequest,
    BookingResponse,
    BookingTransitionRequest,
    ManualReplyCreateRequest,
    ManualReplyData,
    ManualReplyResponse,
)
from app.services.lead_engagement_service import (
    LeadEngagementAssignmentError,
    LeadEngagementConflictError,
    LeadEngagementNotFoundError,
    LeadEngagementPermissionError,
    LeadEngagementReferenceError,
    LeadEngagementStateError,
    cancel_booking,
    confirm_booking,
    create_booking,
    get_booking_history,
    get_bookings,
    record_manual_reply,
    reschedule_booking,
)
from app.services.lead_service import LeadNotFoundError, LeadPermissionError, LeadScopeError

router = APIRouter()


def _raise_engagement_error(exception: Exception) -> None:
    if isinstance(exception, (LeadEngagementNotFoundError, LeadNotFoundError)):
        raise HTTPException(status_code=404, detail="Lead or booking not found") from exception
    if isinstance(exception, LeadEngagementConflictError):
        raise HTTPException(status_code=409, detail="Booking was modified by another request") from exception
    if isinstance(exception, LeadEngagementStateError):
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    if isinstance(exception, LeadEngagementReferenceError):
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    if isinstance(exception, LeadEngagementAssignmentError):
        raise HTTPException(
            status_code=400,
            detail="Responsible user must be an active workspace member",
        ) from exception
    if isinstance(
        exception,
        (LeadEngagementPermissionError, LeadPermissionError, LeadScopeError),
    ):
        raise HTTPException(status_code=403, detail="Lead engagement operation is not authorized") from exception
    raise exception


@router.post(
    "/{lead_id}/replies",
    response_model=ManualReplyResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_manual_reply_route(
    lead_id: uuid.UUID,
    payload: ManualReplyCreateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> ManualReplyResponse:
    try:
        activity = record_manual_reply(database_session, context, lead_id, payload)
    except Exception as exception:
        _raise_engagement_error(exception)
        raise
    return ManualReplyResponse(
        success=True,
        message="Manual reply recorded",
        data=ManualReplyData(
            id=activity.id,
            lead_id=activity.lead_id,
            channel=activity.channel,
            occurred_at=activity.occurred_at,
            summary=activity.notes or "",
            related_outbound_activity_id=activity.related_activity_id,
            recorded_by=activity.recorded_by,
            recorded_at=activity.recorded_at,
            provider_verified=activity.provider_verified,
        ),
    )


@router.post(
    "/{lead_id}/bookings",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_booking_route(
    lead_id: uuid.UUID,
    payload: BookingCreateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> BookingResponse:
    try:
        booking = create_booking(database_session, context, lead_id, payload)
    except Exception as exception:
        _raise_engagement_error(exception)
        raise
    return BookingResponse(
        success=True,
        message="Booking recorded",
        data=BookingData.model_validate(booking),
    )


@router.get("/{lead_id}/bookings", response_model=BookingListResponse)
def list_bookings_route(
    lead_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> BookingListResponse:
    try:
        result = get_bookings(
            database_session, context, lead_id, page=page, size=size
        )
    except Exception as exception:
        _raise_engagement_error(exception)
        raise
    return BookingListResponse(
        success=True,
        data=BookingListData(
            items=[BookingData.model_validate(item) for item in result.items],
            page=page,
            size=size,
            total=result.total,
        ),
    )


@router.get(
    "/{lead_id}/bookings/{booking_id}/history",
    response_model=BookingHistoryResponse,
)
def booking_history_route(
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> BookingHistoryResponse:
    try:
        result = get_booking_history(
            database_session,
            context,
            lead_id,
            booking_id,
            page=page,
            size=size,
        )
    except Exception as exception:
        _raise_engagement_error(exception)
        raise
    return BookingHistoryResponse(
        success=True,
        data=BookingHistoryData(
            items=[BookingEventData.model_validate(item) for item in result.items],
            page=page,
            size=size,
            total=result.total,
        ),
    )


def _booking_response(message: str, booking: object) -> BookingResponse:
    return BookingResponse(
        success=True,
        message=message,
        data=BookingData.model_validate(booking),
    )


@router.post(
    "/{lead_id}/bookings/{booking_id}/confirm",
    response_model=BookingResponse,
)
def confirm_booking_route(
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    payload: BookingTransitionRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> BookingResponse:
    try:
        booking = confirm_booking(
            database_session, context, lead_id, booking_id, payload
        )
    except Exception as exception:
        _raise_engagement_error(exception)
        raise
    return _booking_response("Booking confirmed", booking)


@router.post(
    "/{lead_id}/bookings/{booking_id}/cancel",
    response_model=BookingResponse,
)
def cancel_booking_route(
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    payload: BookingTransitionRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> BookingResponse:
    try:
        booking = cancel_booking(
            database_session, context, lead_id, booking_id, payload
        )
    except Exception as exception:
        _raise_engagement_error(exception)
        raise
    return _booking_response("Booking cancelled", booking)


@router.post(
    "/{lead_id}/bookings/{booking_id}/reschedule",
    response_model=BookingResponse,
)
def reschedule_booking_route(
    lead_id: uuid.UUID,
    booking_id: uuid.UUID,
    payload: BookingRescheduleRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> BookingResponse:
    try:
        booking = reschedule_booking(
            database_session, context, lead_id, booking_id, payload
        )
    except Exception as exception:
        _raise_engagement_error(exception)
        raise
    return _booking_response("Booking rescheduled", booking)
