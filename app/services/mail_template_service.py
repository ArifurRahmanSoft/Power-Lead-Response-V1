import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.permissions import RoleKey
from app.core.security import WorkspaceContext
from app.core.storage import ObjectStorage, StoredObject
from app.models.lead import Lead
from app.models.mail_template import (
    MailTemplate,
    MailTemplateAsset,
    MailTemplateVersion,
    MailTemplateVersionAsset,
)
from app.repositories.mail_template_repository import (
    TemplatePage,
    clone_version_assets,
    count_asset_references,
    get_asset_for_template,
    get_template,
    get_template_version,
    list_templates,
    list_version_assets,
    normalize_template_name,
    template_name_exists,
    unlink_version_asset,
    version_asset_totals,
)
from app.repositories.workspace_repository import add_authorization_audit, lock_tenant
from app.schemas.mail_template import (
    MailTemplateCreateRequest,
    MailTemplatePreviewRequest,
    MailTemplateUpdateRequest,
)
from app.services.lead_service import (
    LeadNotFoundError,
    LeadPermissionError,
    LeadScopeError,
    get_lead_for_context,
)
from app.utils.mail_asset_validation import ValidatedMailAsset
from app.utils.mail_template import (
    PLACEHOLDER_FIELDS,
    UnknownPlaceholderError,
    extract_asset_ids,
    html_to_plain_text,
    lead_placeholder_values,
    render_text_template,
    sanitize_template_html,
    validate_placeholders,
    validate_subject,
)


class MailTemplateNotFoundError(Exception):
    pass


class DuplicateMailTemplateNameError(Exception):
    pass


class TemplateApprovalError(Exception):
    pass


class TemplateAssetError(Exception):
    pass


class TemplateAssetNotFoundError(Exception):
    pass


class TemplateLeadNotFoundError(Exception):
    pass


def _audit(
    database_session: Session,
    context: WorkspaceContext,
    action: str,
    template_id: uuid.UUID,
    details: dict[str, object] | None = None,
) -> None:
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action=action,
        target_membership_id=None,
        details={"template_id": str(template_id), **(details or {})},
    )


def _clean_content(subject: str, body_html: str, signature_html: str) -> dict[str, str]:
    subject = validate_subject(subject)
    body_html, body_text = sanitize_template_html(body_html, required=True)
    signature_html, signature_text = sanitize_template_html(signature_html, required=False)
    validate_placeholders(subject, body_html, signature_html)
    return {
        "subject": subject,
        "body_html": body_html,
        "body_text": body_text,
        "signature_html": signature_html,
        "signature_text": signature_text,
    }


def get_templates(
    database_session: Session,
    context: WorkspaceContext,
    *,
    page: int,
    size: int,
    search: str | None,
    enabled: bool | None,
    approval_status: str | None,
    include_archived: bool,
) -> TemplatePage:
    return list_templates(
        database_session,
        context.tenant.id,
        page=page,
        size=size,
        search=search,
        enabled=enabled,
        approval_status=approval_status,
        include_archived=include_archived,
    )


def get_template_record(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
    *,
    include_archived: bool = False,
    version_number: int | None = None,
) -> tuple[MailTemplate, MailTemplateVersion, list[MailTemplateAsset]]:
    template = get_template(
        database_session,
        context.tenant.id,
        template_id,
        include_archived=include_archived,
    )
    if template is None:
        raise MailTemplateNotFoundError
    version = get_template_version(database_session, template, version_number)
    if version is None:
        raise MailTemplateNotFoundError
    return template, version, list_version_assets(database_session, version.id)


def create_template(
    database_session: Session,
    context: WorkspaceContext,
    payload: MailTemplateCreateRequest,
) -> tuple[MailTemplate, MailTemplateVersion]:
    lock_tenant(database_session, context.tenant.id)
    normalized_name = normalize_template_name(payload.name)
    if template_name_exists(database_session, context.tenant.id, normalized_name):
        database_session.rollback()
        raise DuplicateMailTemplateNameError
    content = _clean_content(payload.subject, payload.body_html, payload.signature_html)
    if extract_asset_ids(content["body_html"]) or extract_asset_ids(content["signature_html"]):
        database_session.rollback()
        raise TemplateAssetError("Upload images before referencing their asset IDs")
    template = MailTemplate(
        tenant_id=context.tenant.id,
        name=payload.name,
        normalized_name=normalized_name,
        enabled=payload.enabled,
        created_by_user_id=context.user.id,
        updated_by_user_id=context.user.id,
    )
    database_session.add(template)
    database_session.flush()
    version = MailTemplateVersion(
        template_id=template.id,
        tenant_id=context.tenant.id,
        version=1,
        approval_status="draft",
        created_by_user_id=context.user.id,
        **content,
    )
    database_session.add(version)
    database_session.flush()
    _audit(database_session, context, "mail_template.created", template.id, {"version": 1})
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        raise DuplicateMailTemplateNameError from exception
    database_session.refresh(template)
    database_session.refresh(version)
    return template, version


def _ensure_draft(
    database_session: Session,
    context: WorkspaceContext,
    template: MailTemplate,
) -> MailTemplateVersion:
    current = get_template_version(database_session, template, for_update=True)
    if current is None:
        raise MailTemplateNotFoundError
    if current.approval_status == "draft":
        return current
    next_number = current.version + 1
    draft = MailTemplateVersion(
        template_id=template.id,
        tenant_id=template.tenant_id,
        version=next_number,
        subject=current.subject,
        body_html=current.body_html,
        body_text=current.body_text,
        signature_html=current.signature_html,
        signature_text=current.signature_text,
        approval_status="draft",
        created_by_user_id=context.user.id,
    )
    database_session.add(draft)
    database_session.flush()
    clone_version_assets(database_session, current.id, draft.id)
    template.current_version = next_number
    template.updated_by_user_id = context.user.id
    return draft


def update_template(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
    payload: MailTemplateUpdateRequest,
) -> tuple[MailTemplate, MailTemplateVersion]:
    lock_tenant(database_session, context.tenant.id)
    template = get_template(
        database_session,
        context.tenant.id,
        template_id,
        for_update=True,
    )
    if template is None:
        database_session.rollback()
        raise MailTemplateNotFoundError
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        normalized_name = normalize_template_name(str(changes["name"]))
        if template_name_exists(
            database_session,
            context.tenant.id,
            normalized_name,
            exclude_id=template.id,
        ):
            database_session.rollback()
            raise DuplicateMailTemplateNameError
        template.name = str(changes["name"])
        template.normalized_name = normalized_name
    if "enabled" in changes:
        template.enabled = bool(changes["enabled"])

    content_fields = {"subject", "body_html", "signature_html"}
    if content_fields & changes.keys():
        version = _ensure_draft(database_session, context, template)
        cleaned = _clean_content(
            str(changes.get("subject", version.subject)),
            str(changes.get("body_html", version.body_html)),
            str(changes.get("signature_html", version.signature_html)),
        )
        for field, value in cleaned.items():
            setattr(version, field, value)
        _validate_referenced_assets(database_session, version)
    else:
        version = get_template_version(database_session, template)
        if version is None:
            raise MailTemplateNotFoundError
    template.updated_by_user_id = context.user.id
    _audit(
        database_session,
        context,
        "mail_template.updated",
        template.id,
        {"version": version.version, "changed_fields": sorted(changes)},
    )
    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        raise DuplicateMailTemplateNameError from exception
    database_session.refresh(template)
    database_session.refresh(version)
    return template, version


def archive_template(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
) -> None:
    lock_tenant(database_session, context.tenant.id)
    template = get_template(
        database_session, context.tenant.id, template_id, for_update=True
    )
    if template is None:
        database_session.rollback()
        raise MailTemplateNotFoundError
    template.archived_at = datetime.now(timezone.utc)
    template.archived_by_user_id = context.user.id
    template.enabled = False
    template.updated_by_user_id = context.user.id
    _audit(database_session, context, "mail_template.archived", template.id)
    database_session.commit()


def _validate_referenced_assets(
    database_session: Session,
    version: MailTemplateVersion,
) -> list[MailTemplateAsset]:
    assets = list_version_assets(database_session, version.id)
    by_id = {asset.id: asset for asset in assets}
    body_ids = extract_asset_ids(version.body_html)
    signature_ids = extract_asset_ids(version.signature_html)
    missing = (body_ids | signature_ids) - by_id.keys()
    if missing:
        raise TemplateApprovalError("Template references missing image assets")
    if any(by_id[asset_id].kind != "inline_body" for asset_id in body_ids):
        raise TemplateApprovalError("Body images must use inline_body assets")
    if any(by_id[asset_id].kind != "signature_image" for asset_id in signature_ids):
        raise TemplateApprovalError("Signature images must use signature_image assets")
    return assets


def _validate_version_assets(
    database_session: Session,
    version: MailTemplateVersion,
) -> list[MailTemplateAsset]:
    assets = _validate_referenced_assets(database_session, version)
    body_ids = extract_asset_ids(version.body_html)
    signature_ids = extract_asset_ids(version.signature_html)
    referenced_images = body_ids | signature_ids
    if any(
        asset.kind in {"inline_body", "signature_image"} and asset.id not in referenced_images
        for asset in assets
    ):
        raise TemplateApprovalError("Every uploaded inline image must be referenced in HTML")
    return assets


def _validate_total_message_size(
    version: MailTemplateVersion,
    assets: list[MailTemplateAsset],
) -> None:
    content_size = sum(
        len(value.encode("utf-8"))
        for value in (
            version.subject,
            version.body_html,
            version.body_text,
            version.signature_html,
            version.signature_text,
        )
    )
    encoded_assets = sum((asset.size_bytes * 4 + 2) // 3 for asset in assets)
    if content_size + encoded_assets + 4096 > settings.mail_template_max_total_bytes:
        raise TemplateApprovalError("Template exceeds the total encoded message size limit")


def approve_template(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
) -> tuple[MailTemplate, MailTemplateVersion]:
    if context.role != RoleKey.OWNER.value:
        raise TemplateApprovalError("Only a workspace Owner can approve templates")
    lock_tenant(database_session, context.tenant.id)
    template = get_template(
        database_session, context.tenant.id, template_id, for_update=True
    )
    if template is None:
        database_session.rollback()
        raise MailTemplateNotFoundError
    version = get_template_version(database_session, template, for_update=True)
    if version is None or version.approval_status != "draft":
        database_session.rollback()
        raise TemplateApprovalError("Only the current draft can be approved")
    assets = _validate_version_assets(database_session, version)
    _validate_total_message_size(version, assets)
    version.approval_status = "approved"
    version.approved_by_user_id = context.user.id
    version.approved_at = datetime.now(timezone.utc)
    template.updated_by_user_id = context.user.id
    _audit(
        database_session,
        context,
        "mail_template.approved",
        template.id,
        {"version": version.version},
    )
    database_session.commit()
    database_session.refresh(template)
    database_session.refresh(version)
    return template, version


def _lead_values(lead: Lead) -> dict[str, object]:
    return lead_placeholder_values(lead)


def preview_template(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
    payload: MailTemplatePreviewRequest,
) -> dict[str, object]:
    template, version, assets = get_template_record(
        database_session,
        context,
        template_id,
        version_number=payload.version,
    )
    _validate_referenced_assets(database_session, version)
    values: dict[str, object] = {}
    warnings: list[str] = []
    if payload.lead_id is not None:
        try:
            values = _lead_values(
                get_lead_for_context(database_session, context, payload.lead_id)
            )
        except (LeadNotFoundError, LeadPermissionError, LeadScopeError) as exception:
            raise TemplateLeadNotFoundError from exception
    elif payload.sample_values is not None:
        unknown = set(payload.sample_values) - PLACEHOLDER_FIELDS.keys()
        if unknown:
            raise UnknownPlaceholderError(unknown)
        values = dict(payload.sample_values)
    else:
        warnings.append("No lead or sample values were supplied; placeholders remain visible.")

    rendered_subject, subject_missing = render_text_template(
        version.subject, values, escape_html=False
    )
    rendered_body, body_missing = render_text_template(
        version.body_html, values, escape_html=True
    )
    rendered_signature, signature_missing = render_text_template(
        version.signature_html, values, escape_html=True
    )
    missing = sorted(subject_missing | body_missing | signature_missing)
    combined_html = rendered_body + (
        f'<div class="mail-signature">{rendered_signature}</div>'
        if rendered_signature
        else ""
    )
    return {
        "template": template,
        "version": version,
        "rendered_subject": rendered_subject,
        "rendered_body_html": rendered_body,
        "rendered_signature_html": rendered_signature,
        "rendered_html": combined_html,
        "rendered_text": html_to_plain_text(combined_html),
        "missing": missing,
        "warnings": warnings,
        "assets": assets,
    }


async def add_template_asset(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
    validated: ValidatedMailAsset,
    kind: str,
    storage: ObjectStorage,
) -> tuple[MailTemplate, MailTemplateVersion, MailTemplateAsset]:
    lock_tenant(database_session, context.tenant.id)
    template = get_template(
        database_session, context.tenant.id, template_id, for_update=True
    )
    if template is None:
        database_session.rollback()
        raise MailTemplateNotFoundError
    version = _ensure_draft(database_session, context, template)
    count, total = version_asset_totals(database_session, version.id)
    if count >= settings.mail_template_max_assets:
        database_session.rollback()
        raise TemplateAssetError("Template asset count limit exceeded")
    content_bytes = sum(
        len(value.encode("utf-8"))
        for value in (
            version.subject,
            version.body_html,
            version.body_text,
            version.signature_html,
            version.signature_text,
        )
    )
    encoded_assets = ((total + len(validated.data)) * 4 + 2) // 3
    if encoded_assets + content_bytes + 4096 > settings.mail_template_max_total_bytes:
        database_session.rollback()
        raise TemplateAssetError("Template total message size limit exceeded")

    asset_id = uuid.uuid4()
    storage_key = f"tenants/{context.tenant.id}/mail-template-assets/{asset_id}"
    await storage.put(storage_key, validated.data, validated.content_type)
    asset = MailTemplateAsset(
        id=asset_id,
        tenant_id=context.tenant.id,
        storage_key=storage_key,
        original_filename=validated.filename,
        content_type=validated.content_type,
        kind=kind,
        size_bytes=len(validated.data),
        sha256=validated.sha256,
        created_by_user_id=context.user.id,
    )
    database_session.add(asset)
    database_session.add(MailTemplateVersionAsset(version_id=version.id, asset_id=asset.id))
    template.updated_by_user_id = context.user.id
    _audit(
        database_session,
        context,
        "mail_template.asset_added",
        template.id,
        {
            "version": version.version,
            "asset_id": str(asset.id),
            "kind": kind,
            "size_bytes": asset.size_bytes,
            "content_type": asset.content_type,
        },
    )
    try:
        database_session.commit()
    except Exception:
        database_session.rollback()
        await storage.delete(storage_key)
        raise
    database_session.refresh(template)
    database_session.refresh(version)
    database_session.refresh(asset)
    return template, version, asset


async def remove_template_asset(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
    asset_id: uuid.UUID,
    storage: ObjectStorage,
) -> None:
    lock_tenant(database_session, context.tenant.id)
    template = get_template(
        database_session, context.tenant.id, template_id, for_update=True
    )
    if template is None:
        database_session.rollback()
        raise MailTemplateNotFoundError
    asset = get_asset_for_template(
        database_session, context.tenant.id, template.id, asset_id
    )
    if asset is None:
        database_session.rollback()
        raise TemplateAssetNotFoundError
    version = _ensure_draft(database_session, context, template)
    if asset.id in extract_asset_ids(version.body_html) | extract_asset_ids(version.signature_html):
        database_session.rollback()
        raise TemplateAssetError("Remove the image from draft HTML before deleting its asset")
    if not unlink_version_asset(database_session, version.id, asset.id):
        database_session.rollback()
        raise TemplateAssetNotFoundError
    database_session.flush()
    should_cleanup = count_asset_references(database_session, asset.id) == 0
    if should_cleanup:
        await storage.delete(asset.storage_key)
        database_session.delete(asset)
    template.updated_by_user_id = context.user.id
    _audit(
        database_session,
        context,
        "mail_template.asset_removed",
        template.id,
        {"version": version.version, "asset_id": str(asset.id)},
    )
    database_session.commit()


async def get_template_asset_content(
    database_session: Session,
    context: WorkspaceContext,
    template_id: uuid.UUID,
    asset_id: uuid.UUID,
    storage: ObjectStorage,
) -> tuple[MailTemplateAsset, StoredObject]:
    template = get_template(
        database_session,
        context.tenant.id,
        template_id,
        include_archived=True,
    )
    if template is None:
        raise MailTemplateNotFoundError
    asset = get_asset_for_template(
        database_session, context.tenant.id, template.id, asset_id
    )
    if asset is None:
        raise TemplateAssetNotFoundError
    return asset, await storage.get(asset.storage_key)
