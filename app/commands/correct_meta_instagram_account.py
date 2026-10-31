import argparse
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.models.tenant import Tenant
from app.models.whatsapp import (
    WhatsAppChannel,
    WhatsAppConversation,
    WhatsAppDeliveryEvent,
    WhatsAppMessage,
)


PROVIDER = "instagram"
ACCOUNT_ID_PATTERN = re.compile(r"^[0-9]{1,32}$")


@dataclass(frozen=True)
class CorrectionResult:
    channel_id: uuid.UUID
    old_channel_id: uuid.UUID
    action: str
    preserved_history: bool
    applied: bool


def _validate_account_id(value: str, argument_name: str) -> str:
    normalized = value.strip()
    if not ACCOUNT_ID_PATTERN.fullmatch(normalized):
        raise RuntimeError(f"{argument_name} must be a numeric Meta account ID")
    return normalized


def _channel_has_history(
    database_session: Session,
    channel_id: uuid.UUID,
) -> bool:
    conversation_exists = database_session.scalar(
        select(WhatsAppConversation.id)
        .where(WhatsAppConversation.channel_id == channel_id)
        .limit(1)
    )
    if conversation_exists is not None:
        return True
    message_exists = database_session.scalar(
        select(WhatsAppMessage.id)
        .join(
            WhatsAppConversation,
            WhatsAppConversation.id == WhatsAppMessage.conversation_id,
        )
        .where(WhatsAppConversation.channel_id == channel_id)
        .limit(1)
    )
    if message_exists is not None:
        return True
    return database_session.scalar(
        select(WhatsAppDeliveryEvent.id)
        .where(WhatsAppDeliveryEvent.channel_id == channel_id)
        .limit(1)
    ) is not None


def _other_active_channel(
    database_session: Session,
    *,
    tenant_id: uuid.UUID,
    excluded_ids: set[uuid.UUID],
) -> WhatsAppChannel | None:
    return database_session.scalar(
        select(WhatsAppChannel)
        .where(
            WhatsAppChannel.tenant_id == tenant_id,
            WhatsAppChannel.provider == PROVIDER,
            WhatsAppChannel.is_active.is_(True),
            WhatsAppChannel.id.not_in(excluded_ids),
        )
        .with_for_update()
        .limit(1)
    )


def correct_account(
    workspace_slug: str,
    old_account_id: str,
    target_account_id: str,
    *,
    apply: bool = False,
) -> CorrectionResult:
    old_id = _validate_account_id(old_account_id, "old_account_id")
    new_id = _validate_account_id(target_account_id, "META_INSTAGRAM_ACCOUNT_ID")
    if old_id == new_id:
        raise RuntimeError(
            "old_account_id and configured META_INSTAGRAM_ACCOUNT_ID must be different"
        )

    def finish(result: CorrectionResult, database_session: Session) -> CorrectionResult:
        if not apply:
            database_session.rollback()
        return result

    with SessionLocal.begin() as database_session:
        tenant = database_session.scalar(
            select(Tenant)
            .where(Tenant.slug == workspace_slug, Tenant.is_active.is_(True))
            .with_for_update()
        )
        if tenant is None:
            raise RuntimeError("No active workspace exists with that exact slug")

        old_channel = database_session.scalar(
            select(WhatsAppChannel)
            .where(
                WhatsAppChannel.provider == PROVIDER,
                WhatsAppChannel.provider_account_id == old_id,
                WhatsAppChannel.provider_channel_id == old_id,
            )
            .with_for_update()
        )
        new_channel = database_session.scalar(
            select(WhatsAppChannel)
            .where(
                WhatsAppChannel.provider == PROVIDER,
                WhatsAppChannel.provider_account_id == new_id,
                WhatsAppChannel.provider_channel_id == new_id,
            )
            .with_for_update()
        )

        if new_channel is not None and new_channel.tenant_id != tenant.id:
            raise RuntimeError(
                "The correct Instagram account is already mapped to another workspace"
            )
        if old_channel is not None and old_channel.tenant_id != tenant.id:
            raise RuntimeError(
                "The incorrect Instagram account belongs to another workspace"
            )

        if old_channel is None:
            if new_channel is None:
                raise RuntimeError(
                    "Neither the incorrect nor correct Instagram mapping exists for this workspace"
                )
            conflict = _other_active_channel(
                database_session,
                tenant_id=tenant.id,
                excluded_ids={new_channel.id},
            )
            if conflict is not None:
                raise RuntimeError(
                    "A different active Instagram mapping still exists for this workspace"
                )
            new_channel.sender_address = new_id
            new_channel.is_active = True
            database_session.flush()
            return finish(
                CorrectionResult(
                    channel_id=new_channel.id,
                    old_channel_id=new_channel.id,
                    action="already_correct",
                    preserved_history=False,
                    applied=apply,
                ),
                database_session,
            )

        excluded = {old_channel.id}
        if new_channel is not None:
            excluded.add(new_channel.id)
        conflict = _other_active_channel(
            database_session,
            tenant_id=tenant.id,
            excluded_ids=excluded,
        )
        if conflict is not None:
            raise RuntimeError(
                "A different active Instagram mapping exists for this workspace"
            )

        has_history = _channel_has_history(database_session, old_channel.id)
        if not has_history and new_channel is None:
            old_channel.provider_account_id = new_id
            old_channel.provider_channel_id = new_id
            old_channel.sender_address = new_id
            old_channel.is_active = True
            database_session.flush()
            return finish(
                CorrectionResult(
                    channel_id=old_channel.id,
                    old_channel_id=old_channel.id,
                    action="corrected_in_place",
                    preserved_history=False,
                    applied=apply,
                ),
                database_session,
            )

        old_channel.is_active = False
        if new_channel is None:
            new_channel = WhatsAppChannel(
                tenant_id=tenant.id,
                provider=PROVIDER,
                provider_account_id=new_id,
                provider_channel_id=new_id,
                twilio_account_sid=None,
                sender_address=new_id,
                is_active=True,
            )
            database_session.add(new_channel)
        else:
            new_channel.sender_address = new_id
            new_channel.is_active = True
        database_session.flush()
        return finish(
            CorrectionResult(
                channel_id=new_channel.id,
                old_channel_id=old_channel.id,
                action="created_new_mapping" if has_history else "activated_existing_mapping",
                preserved_history=has_history,
                applied=apply,
            ),
            database_session,
        )


def correct_configured_account(
    workspace_slug: str,
    old_account_id: str,
    *,
    apply: bool = False,
    configuration: Settings | None = None,
) -> CorrectionResult:
    selected_settings = configuration or get_settings()
    target_account_id = (selected_settings.meta_instagram_account_id or "").strip()
    if not target_account_id:
        raise RuntimeError("META_INSTAGRAM_ACCOUNT_ID must be configured")
    return correct_account(
        workspace_slug,
        old_account_id,
        target_account_id,
        apply=apply,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Transactionally correct one known Meta Instagram account mapping "
            "without relabeling historical channel records"
        )
    )
    parser.add_argument("--workspace-slug", required=True)
    parser.add_argument("--old-account-id", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the correction. Without this flag the command rolls back its dry run.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = correct_configured_account(
        args.workspace_slug,
        args.old_account_id,
        apply=args.apply,
    )
    mode = "applied" if result.applied else "dry-run"
    print(
        "Meta Instagram mapping correction complete "
        f"(mode={mode}, action={result.action}, channel_id={result.channel_id}, "
        f"preserved_history={str(result.preserved_history).lower()})"
    )


if __name__ == "__main__":
    main()
