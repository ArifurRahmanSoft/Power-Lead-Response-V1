import uuid
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.permissions import PermissionKey
from app.core.security import WorkspaceContext, require_permission
from app.core.storage import (
    ObjectStorage,
    ObjectStorageError,
    ObjectStorageNotConfiguredError,
    get_object_storage,
)
from app.models.mail_template import MailTemplate, MailTemplateAsset, MailTemplateVersion
from app.repositories.mail_template_repository import TemplateRecord, list_version_assets
from app.schemas.mail_template import (
    AssetUploadResponse,
    MailTemplateActionResponse,
    MailTemplateAssetData,
    MailTemplateCreateRequest,
    MailTemplateData,
    MailTemplateListData,
    MailTemplateListItem,
    MailTemplateListResponse,
    MailTemplatePreviewData,
    MailTemplatePreviewRequest,
    MailTemplatePreviewResponse,
    MailTemplateResponse,
    MailTemplateUpdateRequest,
    PlaceholderCatalogResponse,
    PlaceholderDefinition,
    TemplateRenderIssue,
)
from app.services.mail_template_service import (
    DuplicateMailTemplateNameError,
    MailTemplateNotFoundError,
    TemplateApprovalError,
    TemplateAssetError,
    TemplateAssetNotFoundError,
    TemplateLeadNotFoundError,
    add_template_asset,
    approve_template,
    archive_template,
    create_template,
    get_template_asset_content,
    get_template_record,
    get_templates,
    preview_template,
    remove_template_asset,
    update_template,
)
from app.utils.mail_asset_validation import InvalidMailAssetError, validate_mail_asset
from app.utils.mail_template import (
    PLACEHOLDER_FIELDS,
    InvalidTemplateSubjectError,
    UnknownPlaceholderError,
    UnsafeTemplateHtmlError,
)

router = APIRouter()


def _asset_data(template_id: uuid.UUID, asset: MailTemplateAsset) -> MailTemplateAssetData:
    placeholder = None
    if asset.kind in {"inline_body", "signature_image"}:
        placeholder = f'<img data-asset-id="{asset.id}" alt="">'
    return MailTemplateAssetData(
        id=asset.id,
        kind=asset.kind,
        filename=asset.original_filename,
        content_type=asset.content_type,
        size_bytes=asset.size_bytes,
        placeholder_html=placeholder,
        content_url=f"/api/mail/templates/{template_id}/assets/{asset.id}/content",
        created_at=asset.created_at,
    )


def _template_data(
    template: MailTemplate,
    version: MailTemplateVersion,
    assets: list[MailTemplateAsset],
) -> MailTemplateData:
    return MailTemplateData(
        id=template.id,
        name=template.name,
        enabled=template.enabled,
        archived=template.archived_at is not None,
        version_id=version.id,
        version=version.version,
        approval_status=version.approval_status,
        subject=version.subject,
        body_html=version.body_html,
        body_text=version.body_text,
        signature_html=version.signature_html,
        signature_text=version.signature_text,
        assets=[_asset_data(template.id, asset) for asset in assets],
        approved_at=version.approved_at,
        created_at=template.created_at,
        updated_at=template.updated_at,
        version_created_at=version.created_at,
        version_updated_at=version.updated_at,
    )


def _list_item(record: TemplateRecord) -> MailTemplateListItem:
    return MailTemplateListItem(
        id=record.template.id,
        name=record.template.name,
        enabled=record.template.enabled,
        archived=record.template.archived_at is not None,
        version=record.version.version,
        approval_status=record.version.approval_status,
        subject=record.version.subject,
        updated_at=record.template.updated_at,
    )


def _raise_template_error(exception: Exception) -> None:
    if isinstance(exception, MailTemplateNotFoundError):
        raise HTTPException(status_code=404, detail="Mail template not found") from exception
    if isinstance(exception, TemplateAssetNotFoundError):
        raise HTTPException(status_code=404, detail="Template asset not found") from exception
    if isinstance(exception, TemplateLeadNotFoundError):
        raise HTTPException(status_code=404, detail="Lead not found or not authorized") from exception
    if isinstance(exception, DuplicateMailTemplateNameError):
        raise HTTPException(
            status_code=409,
            detail="An active mail template with this name already exists",
        ) from exception
    if isinstance(exception, TemplateApprovalError):
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    if isinstance(exception, (TemplateAssetError, InvalidMailAssetError)):
        raise HTTPException(status_code=400, detail=str(exception)) from exception
    if isinstance(exception, UnknownPlaceholderError):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Unknown template placeholders",
                "placeholders": sorted(exception.names),
            },
        ) from exception
    if isinstance(exception, (UnsafeTemplateHtmlError, InvalidTemplateSubjectError)):
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    if isinstance(exception, ObjectStorageNotConfiguredError):
        raise HTTPException(
            status_code=503,
            detail="Mail template object storage is not configured",
        ) from exception
    if isinstance(exception, ObjectStorageError):
        raise HTTPException(status_code=502, detail="Object storage operation failed") from exception
    raise exception


@router.get("/placeholders", response_model=PlaceholderCatalogResponse)
def placeholder_catalog(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_VIEW)),
    ],
) -> PlaceholderCatalogResponse:
    del context
    return PlaceholderCatalogResponse(
        success=True,
        data=[
            PlaceholderDefinition(key=key, label=label)
            for key, label in PLACEHOLDER_FIELDS.items()
        ],
    )


@router.get("", response_model=MailTemplateListResponse)
def list_mail_templates(
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=120)] = None,
    enabled: bool | None = None,
    approval_status: Literal["draft", "approved"] | None = None,
    include_archived: bool = False,
) -> MailTemplateListResponse:
    result = get_templates(
        database_session,
        context,
        page=page,
        size=size,
        search=search,
        enabled=enabled,
        approval_status=approval_status,
        include_archived=include_archived,
    )
    return MailTemplateListResponse(
        success=True,
        data=MailTemplateListData(
            items=[_list_item(record) for record in result.items],
            page=page,
            size=size,
            total=result.total,
        ),
    )


@router.post("", response_model=MailTemplateResponse, status_code=status.HTTP_201_CREATED)
def create_mail_template(
    payload: MailTemplateCreateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MailTemplateResponse:
    try:
        template, version = create_template(database_session, context, payload)
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MailTemplateResponse(
        success=True,
        message="Mail template created",
        data=_template_data(template, version, []),
    )


@router.get("/{template_id}", response_model=MailTemplateResponse)
def get_mail_template(
    template_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    version: Annotated[int | None, Query(ge=1)] = None,
    include_archived: bool = False,
) -> MailTemplateResponse:
    try:
        template, selected_version, assets = get_template_record(
            database_session,
            context,
            template_id,
            include_archived=include_archived,
            version_number=version,
        )
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MailTemplateResponse(
        success=True,
        data=_template_data(template, selected_version, assets),
    )


@router.patch("/{template_id}", response_model=MailTemplateResponse)
def update_mail_template(
    template_id: uuid.UUID,
    payload: MailTemplateUpdateRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MailTemplateResponse:
    try:
        template, version = update_template(
            database_session, context, template_id, payload
        )
        assets = list_version_assets(database_session, version.id)
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MailTemplateResponse(
        success=True,
        message="Mail template updated",
        data=_template_data(template, version, assets),
    )


@router.delete("/{template_id}", response_model=MailTemplateActionResponse)
def archive_mail_template(
    template_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MailTemplateActionResponse:
    try:
        archive_template(database_session, context, template_id)
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MailTemplateActionResponse(success=True, message="Mail template archived")


@router.post("/{template_id}/approve", response_model=MailTemplateResponse)
def approve_mail_template(
    template_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_APPROVE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MailTemplateResponse:
    try:
        template, version = approve_template(
            database_session, context, template_id
        )
        assets = list_version_assets(database_session, version.id)
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MailTemplateResponse(
        success=True,
        message="Mail template version approved",
        data=_template_data(template, version, assets),
    )


@router.post("/{template_id}/preview", response_model=MailTemplatePreviewResponse)
def preview_mail_template(
    template_id: uuid.UUID,
    payload: MailTemplatePreviewRequest,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
) -> MailTemplatePreviewResponse:
    try:
        result = preview_template(
            database_session, context, template_id, payload
        )
    except Exception as exception:
        _raise_template_error(exception)
        raise
    template = result["template"]
    version = result["version"]
    assets = result["assets"]
    return MailTemplatePreviewResponse(
        success=True,
        data=MailTemplatePreviewData(
            template_id=template.id,
            version=version.version,
            rendered_subject=result["rendered_subject"],
            rendered_body_html=result["rendered_body_html"],
            rendered_signature_html=result["rendered_signature_html"],
            rendered_html=result["rendered_html"],
            rendered_text=result["rendered_text"],
            missing_values=[
                TemplateRenderIssue(
                    placeholder=name,
                    message="A value is required before this template can be sent",
                )
                for name in result["missing"]
            ],
            warnings=result["warnings"],
            asset_urls={
                str(asset.id): f"/api/mail/templates/{template.id}/assets/{asset.id}/content"
                for asset in assets
                if asset.kind in {"inline_body", "signature_image"}
            },
        ),
    )


@router.post(
    "/{template_id}/assets",
    response_model=AssetUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_template_asset(
    template_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    kind: Annotated[
        Literal["inline_body", "signature_image", "attachment"], Form()
    ],
    file: Annotated[UploadFile, File()],
) -> AssetUploadResponse:
    try:
        data = await file.read(settings.mail_template_asset_max_file_bytes + 1)
    finally:
        await file.close()
    if len(data) > settings.mail_template_asset_max_file_bytes:
        raise HTTPException(status_code=413, detail="Template asset is too large")
    try:
        validated = validate_mail_asset(file.filename, data, kind=kind)
        template, version, asset = await add_template_asset(
            database_session,
            context,
            template_id,
            validated,
            kind,
            storage,
        )
        del template, version
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return AssetUploadResponse(
        success=True,
        message="Template asset uploaded",
        data=_asset_data(template_id, asset),
    )


@router.get("/{template_id}/assets/{asset_id}/content")
async def template_asset_content(
    template_id: uuid.UUID,
    asset_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_VIEW)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> Response:
    try:
        asset, stored = await get_template_asset_content(
            database_session,
            context,
            template_id,
            asset_id,
            storage,
        )
    except Exception as exception:
        _raise_template_error(exception)
        raise
    disposition = "inline" if asset.kind != "attachment" else "attachment"
    return Response(
        content=stored.data,
        media_type=asset.content_type,
        headers={
            "Content-Disposition": (
                f"{disposition}; filename*=UTF-8''{quote(asset.original_filename)}"
            ),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete(
    "/{template_id}/assets/{asset_id}",
    response_model=MailTemplateActionResponse,
)
async def delete_template_asset(
    template_id: uuid.UUID,
    asset_id: uuid.UUID,
    context: Annotated[
        WorkspaceContext,
        Depends(require_permission(PermissionKey.MAIL_TEMPLATES_MANAGE)),
    ],
    database_session: Annotated[Session, Depends(get_db)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> MailTemplateActionResponse:
    try:
        await remove_template_asset(
            database_session,
            context,
            template_id,
            asset_id,
            storage,
        )
    except Exception as exception:
        _raise_template_error(exception)
        raise
    return MailTemplateActionResponse(success=True, message="Template asset removed")
