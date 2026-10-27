import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.schemas.customer import (
    CustomerData,
    CustomerDetailData,
    CustomerDetailResponse,
    CustomerListData,
    CustomerListResponse,
    LeadConversionData,
    LeadConversionRequest,
    LeadConversionResponse,
    LeadConversionResponseData,
)
from app.services.customer_service import (
    CustomerConversionConflictError,
    CustomerConversionLeadNotFoundError,
    CustomerConversionPermissionError,
    CustomerConversionStateError,
    CustomerDuplicateError,
    CustomerNotFoundError,
    convert_lead_to_customer,
    get_customer_detail,
    get_customers,
)

lead_conversion_router = APIRouter()
customer_router = APIRouter()


def _raise_customer_error(exception: Exception) -> None:
    if isinstance(
        exception,
        (CustomerNotFoundError, CustomerConversionLeadNotFoundError),
    ):
        raise HTTPException(status_code=404, detail="Customer or lead not found") from exception
    if isinstance(exception, CustomerConversionPermissionError):
        raise HTTPException(status_code=403, detail="Customer operation is not authorized") from exception
    if isinstance(exception, CustomerConversionConflictError):
        raise HTTPException(
            status_code=409,
            detail="Lead was modified by another request",
        ) from exception
    if isinstance(exception, CustomerConversionStateError):
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    if isinstance(exception, CustomerDuplicateError):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "A customer with this email already exists; retry with link_existing",
                "existing_customer_id": str(exception.customer.id),
            },
        ) from exception
    raise exception


@lead_conversion_router.post(
    "/{lead_id}/convert",
    response_model=LeadConversionResponse,
)
def convert_lead_route(
    lead_id: uuid.UUID,
    payload: LeadConversionRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.CUSTOMERS_CONVERT)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> LeadConversionResponse:
    try:
        result = convert_lead_to_customer(
            database_session,
            context,
            lead_id,
            payload,
        )
    except Exception as exception:
        _raise_customer_error(exception)
        raise
    return LeadConversionResponse(
        success=True,
        message=(
            "Lead was already converted"
            if result.idempotent_replay
            else "Lead converted to customer"
        ),
        data=LeadConversionResponseData(
            customer=CustomerData.model_validate(result.customer),
            conversion=LeadConversionData.model_validate(result.conversion),
            idempotent_replay=result.idempotent_replay,
        ),
    )


@customer_router.get("", response_model=CustomerListResponse)
def list_customers_route(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.CUSTOMERS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> CustomerListResponse:
    result = get_customers(
        database_session,
        context,
        page=page,
        size=size,
        search=search,
    )
    return CustomerListResponse(
        success=True,
        data=CustomerListData(
            items=[CustomerData.model_validate(item) for item in result.items],
            page=page,
            size=size,
            total=result.total,
        ),
    )


@customer_router.get("/{customer_id}", response_model=CustomerDetailResponse)
def get_customer_route(
    customer_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.CUSTOMERS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> CustomerDetailResponse:
    try:
        detail = get_customer_detail(database_session, context, customer_id)
    except Exception as exception:
        _raise_customer_error(exception)
        raise
    customer_data = CustomerData.model_validate(detail.customer)
    return CustomerDetailResponse(
        success=True,
        data=CustomerDetailData(
            **customer_data.model_dump(),
            conversions=[
                LeadConversionData.model_validate(conversion)
                for conversion in detail.conversions
            ],
        ),
    )
