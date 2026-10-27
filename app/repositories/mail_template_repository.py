import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models.mail_template import (
    MailTemplate,
    MailTemplateAsset,
    MailTemplateVersion,
    MailTemplateVersionAsset,
)


@dataclass(frozen=True)
class TemplateRecord:
    template: MailTemplate
    version: MailTemplateVersion


@dataclass(frozen=True)
class TemplatePage:
    items: list[TemplateRecord]
    total: int


def normalize_template_name(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def template_name_exists(
    database_session: Session,
    tenant_id: uuid.UUID,
    normalized_name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    statement = select(MailTemplate.id).where(
        MailTemplate.tenant_id == tenant_id,
        MailTemplate.normalized_name == normalized_name,
        MailTemplate.archived_at.is_(None),
    )
    if exclude_id is not None:
        statement = statement.where(MailTemplate.id != exclude_id)
    return database_session.scalar(statement) is not None


def list_templates(
    database_session: Session,
    tenant_id: uuid.UUID,
    *,
    page: int,
    size: int,
    search: str | None,
    enabled: bool | None,
    approval_status: str | None,
    include_archived: bool,
) -> TemplatePage:
    conditions = [MailTemplate.tenant_id == tenant_id]
    if not include_archived:
        conditions.append(MailTemplate.archived_at.is_(None))
    if enabled is not None:
        conditions.append(MailTemplate.enabled.is_(enabled))
    if approval_status is not None:
        conditions.append(MailTemplateVersion.approval_status == approval_status)
    if search and search.strip():
        term = f"%{search.strip()}%"
        conditions.append(
            or_(MailTemplate.name.ilike(term), MailTemplateVersion.subject.ilike(term))
        )
    joined = (
        select(MailTemplate, MailTemplateVersion)
        .join(
            MailTemplateVersion,
            (MailTemplateVersion.template_id == MailTemplate.id)
            & (MailTemplateVersion.version == MailTemplate.current_version),
        )
        .where(*conditions)
    )
    total = database_session.scalar(
        select(func.count()).select_from(joined.subquery())
    ) or 0
    rows = database_session.execute(
        joined.order_by(MailTemplate.updated_at.desc(), MailTemplate.id)
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return TemplatePage(
        items=[TemplateRecord(row[0], row[1]) for row in rows],
        total=total,
    )


def get_template(
    database_session: Session,
    tenant_id: uuid.UUID,
    template_id: uuid.UUID,
    *,
    include_archived: bool = False,
    for_update: bool = False,
) -> MailTemplate | None:
    statement = select(MailTemplate).where(
        MailTemplate.id == template_id,
        MailTemplate.tenant_id == tenant_id,
    )
    if not include_archived:
        statement = statement.where(MailTemplate.archived_at.is_(None))
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def get_template_version(
    database_session: Session,
    template: MailTemplate,
    version_number: int | None = None,
    *,
    for_update: bool = False,
) -> MailTemplateVersion | None:
    statement = select(MailTemplateVersion).where(
        MailTemplateVersion.template_id == template.id,
        MailTemplateVersion.tenant_id == template.tenant_id,
        MailTemplateVersion.version == (version_number or template.current_version),
    )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def list_version_assets(
    database_session: Session,
    version_id: uuid.UUID,
) -> list[MailTemplateAsset]:
    return list(
        database_session.scalars(
            select(MailTemplateAsset)
            .join(
                MailTemplateVersionAsset,
                MailTemplateVersionAsset.asset_id == MailTemplateAsset.id,
            )
            .where(MailTemplateVersionAsset.version_id == version_id)
            .order_by(MailTemplateAsset.kind, MailTemplateAsset.original_filename, MailTemplateAsset.id)
        ).all()
    )


def clone_version_assets(
    database_session: Session,
    source_version_id: uuid.UUID,
    target_version_id: uuid.UUID,
) -> None:
    asset_ids = database_session.scalars(
        select(MailTemplateVersionAsset.asset_id).where(
            MailTemplateVersionAsset.version_id == source_version_id
        )
    ).all()
    database_session.add_all(
        MailTemplateVersionAsset(version_id=target_version_id, asset_id=asset_id)
        for asset_id in asset_ids
    )


def get_asset_for_template(
    database_session: Session,
    tenant_id: uuid.UUID,
    template_id: uuid.UUID,
    asset_id: uuid.UUID,
) -> MailTemplateAsset | None:
    return database_session.scalar(
        select(MailTemplateAsset)
        .join(
            MailTemplateVersionAsset,
            MailTemplateVersionAsset.asset_id == MailTemplateAsset.id,
        )
        .join(
            MailTemplateVersion,
            MailTemplateVersion.id == MailTemplateVersionAsset.version_id,
        )
        .where(
            MailTemplateAsset.id == asset_id,
            MailTemplateAsset.tenant_id == tenant_id,
            MailTemplateVersion.template_id == template_id,
        )
        .limit(1)
    )


def version_asset_totals(
    database_session: Session,
    version_id: uuid.UUID,
) -> tuple[int, int]:
    row = database_session.execute(
        select(
            func.count(MailTemplateAsset.id),
            func.coalesce(func.sum(MailTemplateAsset.size_bytes), 0),
        )
        .join(
            MailTemplateVersionAsset,
            MailTemplateVersionAsset.asset_id == MailTemplateAsset.id,
        )
        .where(MailTemplateVersionAsset.version_id == version_id)
    ).one()
    return int(row[0]), int(row[1])


def unlink_version_asset(
    database_session: Session,
    version_id: uuid.UUID,
    asset_id: uuid.UUID,
) -> bool:
    result = database_session.execute(
        delete(MailTemplateVersionAsset).where(
            MailTemplateVersionAsset.version_id == version_id,
            MailTemplateVersionAsset.asset_id == asset_id,
        )
    )
    return bool(result.rowcount)


def count_asset_references(database_session: Session, asset_id: uuid.UUID) -> int:
    return database_session.scalar(
        select(func.count(MailTemplateVersionAsset.version_id)).where(
            MailTemplateVersionAsset.asset_id == asset_id
        )
    ) or 0
