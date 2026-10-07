import uuid
from io import BytesIO
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.rate_limit import RateLimitExceededError
from app.core.security import WorkspaceContext, require_permission
from app.imports.lead_excel import LeadImportFileError, build_import_template
from app.models.lead import Lead, LeadImportBatch, LeadImportRow
from app.providers.groq import (
    GroqInvalidAudioError,
    GroqMalformedResponseError,
    GroqNotConfiguredError,
    GroqProvider,
    GroqProviderError,
    GroqRateLimitError,
    GroqTimeoutError,
    get_groq_provider,
)
from app.schemas.lead import (
    LeadCreateRequest,
    LeadData,
    LeadDeleteResponse,
    LeadFilterParams,
    LeadImportBatchData,
    LeadImportBatchResponse,
    LeadImportErrorsData,
    LeadImportErrorsResponse,
    LeadImportRowData,
    LeadImportRowError,
    LeadListData,
    LeadListResponse,
    LeadResponse,
    LeadServiceOption,
    LeadServiceOptionsData,
    LeadServiceOptionsResponse,
    LeadUpdateRequest,
)
from app.schemas.lead_voice import LeadVoiceExtractResponse
from app.services.lead_import_service import (
    LeadImportExpiredError,
    LeadImportNotFoundError,
    LeadImportStateError,
    commit_import,
    get_batch,
    get_batch_errors,
    preview_import,
)
from app.services.lead_voice_service import (
    EmptyLeadAudioError,
    LeadAudioTooLargeError,
    LeadAudioTooLongError,
    SilentLeadAudioError,
    UnsupportedLeadAudioError,
    extract_lead_voice_draft,
)
from app.services.lead_service import (
    LeadAssignmentError,
    LeadConflictError,
    LeadDuplicateError,
    LeadNotFoundError,
    LeadPermissionError,
    LeadScopeError,
    LeadTransitionError,
    archive_lead,
    create_lead,
    get_lead_for_context,
    get_leads,
    get_service_filter_options,
    update_lead,
)

router = APIRouter()


def _lead_data(lead: Lead) -> LeadData:
    return LeadData.model_validate(lead)


def _batch_data(batch: LeadImportBatch) -> LeadImportBatchData:
    return LeadImportBatchData(
        id=batch.id,
        status=batch.status,
        file_name=batch.file_name,
        mapping=batch.mapping,
        summary=batch.summary,
        expires_at=batch.expires_at,
        created_at=batch.created_at,
        committed_at=batch.committed_at,
    )


def _row_data(row: LeadImportRow) -> LeadImportRowData:
    return LeadImportRowData(
        row_number=row.row_number,
        status=row.status,
        errors=[LeadImportRowError.model_validate(error) for error in row.errors],
        lead_id=row.lead_id,
    )


def _raise_lead_error(exception: Exception) -> None:
    if isinstance(exception, LeadNotFoundError):
        raise HTTPException(status_code=404, detail="Lead not found") from exception
    if isinstance(exception, LeadDuplicateError):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "An active lead with this email already exists",
                "existing_lead": {
                    "id": str(exception.existing.id),
                    "tracking_id": exception.existing.tracking_id,
                },
            },
        ) from exception
    if isinstance(exception, LeadConflictError):
        raise HTTPException(status_code=409, detail="Lead was modified by another request") from exception
    if isinstance(exception, LeadAssignmentError):
        raise HTTPException(status_code=400, detail="Assignee must be an active member of this workspace") from exception
    if isinstance(exception, LeadTransitionError):
        raise HTTPException(status_code=400, detail="Invalid lead state or field combination") from exception
    if isinstance(exception, (LeadScopeError, LeadPermissionError)):
        raise HTTPException(status_code=403, detail="Lead operation is not authorized") from exception
    raise exception


@router.get("/import-template")
def download_import_template(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_IMPORT)),
    ],
) -> StreamingResponse:
    del context
    return StreamingResponse(
        BytesIO(build_import_template()),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="lead-import-template.xlsx"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/imports/preview", response_model=LeadImportBatchResponse, status_code=201)
async def preview_lead_import(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_IMPORT)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="A macro-free .xlsx workbook")],
    mapping: Annotated[str | None, Form()] = None,
) -> LeadImportBatchResponse:
    file_bytes = await file.read(settings.lead_import_max_file_bytes + 1)
    try:
        batch = preview_import(
            database_session,
            context,
            file_name=file.filename or "upload.xlsx",
            file_bytes=file_bytes,
            mapping_json=mapping,
        )
    except LeadImportFileError as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    return LeadImportBatchResponse(success=True, message="Import preview created", data=_batch_data(batch))


@router.post("/imports/{batch_id}/commit", response_model=LeadImportBatchResponse)
def commit_lead_import(
    batch_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_IMPORT)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> LeadImportBatchResponse:
    try:
        batch = commit_import(database_session, context, batch_id)
    except LeadImportNotFoundError as exception:
        raise HTTPException(status_code=404, detail="Import batch not found") from exception
    except LeadImportExpiredError as exception:
        raise HTTPException(status_code=409, detail="Import preview has expired") from exception
    except LeadImportStateError as exception:
        raise HTTPException(status_code=409, detail="Import batch cannot be committed in its current state") from exception
    return LeadImportBatchResponse(success=True, message="Import committed", data=_batch_data(batch))


@router.get("/imports/{batch_id}", response_model=LeadImportBatchResponse)
def read_lead_import(
    batch_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_IMPORT)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> LeadImportBatchResponse:
    try:
        batch = get_batch(database_session, context, batch_id)
    except LeadImportNotFoundError as exception:
        raise HTTPException(status_code=404, detail="Import batch not found") from exception
    return LeadImportBatchResponse(success=True, data=_batch_data(batch))


@router.get("/imports/{batch_id}/errors", response_model=LeadImportErrorsResponse)
def read_lead_import_errors(
    batch_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_IMPORT)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> LeadImportErrorsResponse:
    try:
        result = get_batch_errors(database_session, context, batch_id, page=page, size=size)
    except LeadImportNotFoundError as exception:
        raise HTTPException(status_code=404, detail="Import batch not found") from exception
    return LeadImportErrorsResponse(
        success=True,
        data=LeadImportErrorsData(
            items=[_row_data(row) for row in result.items],
            page=page,
            size=size,
            total=result.total,
        ),
    )


@router.post("", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
def create_lead_route(
    payload: LeadCreateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_CREATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> LeadResponse:
    try:
        lead = create_lead(database_session, context, payload)
    except Exception as exception:
        _raise_lead_error(exception)
        raise
    return LeadResponse(success=True, message="Lead created", data=_lead_data(lead))


@router.get("", response_model=LeadListResponse)
def list_leads_route(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: str | None = None,
    name: str | None = None,
    email: str | None = None,
    company: str | None = None,
    country: str | None = None,
    industry: str | None = None,
    service_requested: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    email_status: str | None = None,
    source: str | None = None,
    assigned_user_id: uuid.UUID | None = None,
) -> LeadListResponse:
    try:
        filters = LeadFilterParams(
            search=search,
            name=name,
            email=email,
            company=company,
            country=country,
            industry=industry,
            service_requested=service_requested,
            status=status_filter,
            email_status=email_status,
            source=source,
            assigned_user_id=assigned_user_id,
        )
        result = get_leads(database_session, context, page=page, size=size, filters=filters)
    except ValueError as exception:
        raise HTTPException(status_code=422, detail="Invalid lead filter") from exception
    return LeadListResponse(
        success=True,
        data=LeadListData(
            items=[_lead_data(lead) for lead in result.items],
            page=page,
            size=size,
            total=result.total,
        ),
    )


@router.get(
    "/service-requested-options",
    response_model=LeadServiceOptionsResponse,
)
def list_lead_service_options_route(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 50,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> LeadServiceOptionsResponse:
    result = get_service_filter_options(
        database_session,
        context,
        page=page,
        size=size,
        search=search,
    )
    return LeadServiceOptionsResponse(
        success=True,
        data=LeadServiceOptionsData(
            items=[
                LeadServiceOption(label=item.label, value=item.value)
                for item in result.items
            ],
            page=page,
            size=size,
            total=result.total,
        ),
    )


@router.post("/voice-extract", response_model=LeadVoiceExtractResponse)
async def extract_lead_from_voice_route(
    audio: Annotated[UploadFile, File(description="Supported lead dictation audio")],
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_VIEW)),
    ],
    provider: Annotated[GroqProvider, Depends(get_groq_provider)],
) -> LeadVoiceExtractResponse:
    try:
        return await extract_lead_voice_draft(
            audio,
            provider,
            requester_key=f"{context.tenant.id}:{context.user.id}",
        )
    except UnsupportedLeadAudioError as exception:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported audio type",
        ) from exception
    except LeadAudioTooLargeError as exception:
        raise HTTPException(status_code=413, detail="Audio file is too large") from exception
    except LeadAudioTooLongError as exception:
        raise HTTPException(
            status_code=422,
            detail="Audio duration exceeds the configured limit",
        ) from exception
    except (EmptyLeadAudioError, SilentLeadAudioError, GroqInvalidAudioError) as exception:
        raise HTTPException(
            status_code=422,
            detail="Audio is empty, silent, or invalid",
        ) from exception
    except (RateLimitExceededError, GroqRateLimitError) as exception:
        raise HTTPException(
            status_code=429,
            detail="Voice extraction rate limit exceeded; try again later",
        ) from exception
    except GroqNotConfiguredError as exception:
        raise HTTPException(
            status_code=503,
            detail="Voice extraction is not configured",
        ) from exception
    except GroqTimeoutError as exception:
        raise HTTPException(
            status_code=504,
            detail="Voice extraction provider timed out",
        ) from exception
    except (GroqMalformedResponseError, GroqProviderError) as exception:
        raise HTTPException(
            status_code=502,
            detail="Voice extraction provider is unavailable",
        ) from exception


@router.get("/{lead_id}", response_model=LeadResponse)
def get_lead_route(
    lead_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> LeadResponse:
    try:
        lead = get_lead_for_context(database_session, context, lead_id)
    except Exception as exception:
        _raise_lead_error(exception)
        raise
    return LeadResponse(success=True, data=_lead_data(lead))


@router.patch("/{lead_id}", response_model=LeadResponse)
def update_lead_route(
    lead_id: uuid.UUID,
    payload: LeadUpdateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_UPDATE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> LeadResponse:
    try:
        lead = update_lead(database_session, context, lead_id, payload)
    except Exception as exception:
        _raise_lead_error(exception)
        raise
    return LeadResponse(success=True, message="Lead updated", data=_lead_data(lead))


@router.delete("/{lead_id}", response_model=LeadDeleteResponse)
def delete_lead_route(
    lead_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.LEADS_DELETE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    row_version: Annotated[int, Query(ge=1)],
) -> LeadDeleteResponse:
    try:
        archive_lead(database_session, context, lead_id, row_version)
    except Exception as exception:
        _raise_lead_error(exception)
        raise
    return LeadDeleteResponse(success=True, message="Lead archived")
