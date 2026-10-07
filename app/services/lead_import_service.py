import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.lead_catalogs import LeadSource, LeadStatus
from app.core.permissions import RoleKey
from app.core.security import WorkspaceContext
from app.imports.lead_excel import LeadImportFileError, parse_workbook
from app.models.lead import Lead, LeadImportBatch, LeadImportRow
from app.repositories.lead_repository import (
    Page,
    add_lead_event,
    get_active_duplicate,
    get_existing_active_emails,
    get_import_batch,
    get_import_batch_by_hash,
    list_import_error_rows,
    list_import_rows,
)
from app.repositories.workspace_repository import add_authorization_audit
from app.schemas.lead import LeadFields, validation_errors
from app.utils.lead_normalization import country_name_to_code


class LeadImportNotFoundError(Exception):
    pass


class LeadImportExpiredError(Exception):
    pass


class LeadImportStateError(Exception):
    pass


def _get_owned_batch(
    database_session: Session,
    context: WorkspaceContext,
    batch_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> LeadImportBatch:
    batch = get_import_batch(
        database_session,
        tenant_id=context.tenant.id,
        batch_id=batch_id,
        for_update=for_update,
    )
    if batch is None:
        raise LeadImportNotFoundError
    if context.role != RoleKey.OWNER.value and batch.created_by != context.user.id:
        raise LeadImportNotFoundError
    return batch


def preview_import(
    database_session: Session,
    context: WorkspaceContext,
    *,
    file_name: str,
    file_bytes: bytes,
    mapping_json: str | None,
) -> LeadImportBatch:
    if not file_name.casefold().endswith(".xlsx"):
        raise LeadImportFileError("Only .xlsx files are supported")
    if len(file_bytes) > settings.lead_import_max_file_bytes:
        raise LeadImportFileError(
            f"File exceeds the {settings.lead_import_max_file_bytes} byte limit"
        )
    parsed = parse_workbook(
        file_bytes,
        mapping_json=mapping_json,
        max_rows=settings.lead_import_max_rows,
    )
    snapshot_hash = hashlib.sha256(
        (
            parsed.file_sha256
            + json.dumps(parsed.mapping, sort_keys=True, separators=(",", ":"))
        ).encode("utf-8")
    ).hexdigest()
    existing_batch = get_import_batch_by_hash(
        database_session,
        tenant_id=context.tenant.id,
        created_by=context.user.id,
        snapshot_hash=snapshot_hash,
    )
    if existing_batch is not None:
        return existing_batch

    candidate_emails = {
        str(row.data.get("email", ""))
        for row in parsed.rows
        if not row.errors and row.data.get("email")
    }
    existing_emails = get_existing_active_emails(
        database_session,
        context.tenant.id,
        candidate_emails,
    )
    seen_emails: set[str] = set()
    summary = {"total": len(parsed.rows), "valid": 0, "invalid": 0, "duplicate": 0, "inserted": 0, "failed": 0}
    batch = LeadImportBatch(
        tenant_id=context.tenant.id,
        created_by=context.user.id,
        file_name=file_name[:255],
        file_sha256=parsed.file_sha256,
        snapshot_hash=snapshot_hash,
        mapping=parsed.mapping,
        summary=summary,
        status="previewed",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.lead_import_retention_hours),
    )
    database_session.add(batch)
    database_session.flush()

    for parsed_row in parsed.rows:
        errors = list(parsed_row.errors)
        email = str(parsed_row.data.get("email", ""))
        if errors:
            row_status = "invalid"
        elif email in existing_emails or email in seen_emails:
            row_status = "duplicate"
            errors.append(
                {
                    "field": "email",
                    "message": "Active email already exists in this tenant or import",
                }
            )
        else:
            row_status = "valid"
            seen_emails.add(email)
        summary[row_status] += 1
        database_session.add(
            LeadImportRow(
                batch_id=batch.id,
                tenant_id=context.tenant.id,
                row_number=parsed_row.row_number,
                row_hash=parsed_row.row_hash,
                normalized_data=parsed_row.data,
                status=row_status,
                errors=errors,
            )
        )
    batch.summary = dict(summary)
    flag_modified(batch, "summary")
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.import.previewed",
        target_membership_id=context.membership.id,
        details={"batch_id": str(batch.id), "summary": dict(summary)},
    )
    try:
        database_session.commit()
    except IntegrityError:
        database_session.rollback()
        existing_batch = get_import_batch_by_hash(
            database_session,
            tenant_id=context.tenant.id,
            created_by=context.user.id,
            snapshot_hash=snapshot_hash,
        )
        if existing_batch is not None:
            return existing_batch
        raise
    database_session.refresh(batch)
    return batch


def _create_imported_lead(
    database_session: Session,
    context: WorkspaceContext,
    batch: LeadImportBatch,
    row: LeadImportRow,
) -> Lead:
    raw_data = dict(row.normalized_data)
    original_email = str(raw_data.pop("email_original", raw_data.get("email", "")))
    validated = LeadFields.model_validate(raw_data)
    values = validated.model_dump(mode="python")
    values["country"] = country_name_to_code(values.get("country"))
    lead = Lead(
        **values,
        tenant_id=context.tenant.id,
        tracking_id=f"LD-{uuid.uuid4().hex.upper()}",
        email_original=original_email,
        source=LeadSource.IMPORT.value,
        status=LeadStatus.NEW.value,
        import_batch_id=batch.id,
        created_by=context.user.id,
        updated_by=context.user.id,
    )
    database_session.add(lead)
    database_session.flush()
    add_lead_event(
        database_session,
        lead=lead,
        actor_user_id=context.user.id,
        event_type="lead.created",
        details={"source": LeadSource.IMPORT.value, "batch_id": str(batch.id), "row_number": row.row_number},
    )
    return lead


def commit_import(
    database_session: Session,
    context: WorkspaceContext,
    batch_id: uuid.UUID,
) -> LeadImportBatch:
    batch = _get_owned_batch(database_session, context, batch_id, for_update=True)
    if batch.status in {"completed", "completed_with_errors"}:
        return batch
    now = datetime.now(timezone.utc)
    expires_at = batch.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        raise LeadImportExpiredError
    if batch.status != "previewed":
        raise LeadImportStateError

    batch.status = "committing"
    database_session.flush()
    rows = list_import_rows(database_session, batch.id, statuses={"valid"})
    for row in rows:
        email = str(row.normalized_data.get("email", ""))
        if get_active_duplicate(database_session, context.tenant.id, email) is not None:
            row.status = "duplicate"
            row.errors = [{"field": "email", "message": "Active email already exists in this tenant"}]
            continue
        try:
            with database_session.begin_nested():
                lead = _create_imported_lead(database_session, context, batch, row)
                row.lead_id = lead.id
                row.status = "inserted"
        except IntegrityError:
            row.status = "duplicate"
            row.errors = [{"field": "email", "message": "Active email already exists in this tenant"}]
        except ValidationError as exception:
            row.status = "failed"
            row.errors = validation_errors(exception)

    all_rows = list_import_rows(database_session, batch.id)
    summary = {
        "total": len(all_rows),
        "valid": sum(row.status == "valid" for row in all_rows),
        "invalid": sum(row.status == "invalid" for row in all_rows),
        "duplicate": sum(row.status == "duplicate" for row in all_rows),
        "inserted": sum(row.status == "inserted" for row in all_rows),
        "failed": sum(row.status == "failed" for row in all_rows),
    }
    batch.summary = summary
    flag_modified(batch, "summary")
    batch.status = "completed_with_errors" if summary["failed"] else "completed"
    batch.committed_at = now
    add_authorization_audit(
        database_session,
        tenant_id=context.tenant.id,
        actor_user_id=context.user.id,
        action="lead.import.committed",
        target_membership_id=context.membership.id,
        details={"batch_id": str(batch.id), "summary": summary},
    )
    database_session.commit()
    database_session.refresh(batch)
    return batch


def get_batch(
    database_session: Session,
    context: WorkspaceContext,
    batch_id: uuid.UUID,
) -> LeadImportBatch:
    return _get_owned_batch(database_session, context, batch_id)


def get_batch_errors(
    database_session: Session,
    context: WorkspaceContext,
    batch_id: uuid.UUID,
    *,
    page: int,
    size: int,
) -> Page:
    batch = _get_owned_batch(database_session, context, batch_id)
    return list_import_error_rows(database_session, batch.id, page=page, size=size)
