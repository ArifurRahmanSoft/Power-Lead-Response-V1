import argparse

from sqlalchemy import select

from app.core.config import Settings
from app.core.database import SessionLocal
from app.models.tenant import Tenant
from app.models.whatsapp import WhatsAppChannel


def configure_page(workspace_slug: str) -> tuple[WhatsAppChannel, bool]:
    settings = Settings()
    page_id = (settings.meta_messenger_page_id or "").strip()
    if not page_id:
        raise RuntimeError("META_MESSENGER_PAGE_ID must be configured")
    with SessionLocal.begin() as database_session:
        tenant = database_session.scalar(
            select(Tenant).where(
                Tenant.slug == workspace_slug,
                Tenant.is_active.is_(True),
            )
        )
        if tenant is None:
            raise RuntimeError("No active workspace exists with that exact slug")
        existing = database_session.scalar(
            select(WhatsAppChannel)
            .where(
                WhatsAppChannel.provider == "messenger",
                WhatsAppChannel.provider_account_id == page_id,
                WhatsAppChannel.provider_channel_id == page_id,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.tenant_id != tenant.id:
                raise RuntimeError(
                    "The configured Facebook Page is already mapped to another workspace"
                )
            existing.sender_address = page_id
            existing.is_active = True
            database_session.flush()
            return existing, False
        other = database_session.scalar(
            select(WhatsAppChannel).where(
                WhatsAppChannel.tenant_id == tenant.id,
                WhatsAppChannel.provider == "messenger",
                WhatsAppChannel.is_active.is_(True),
            )
        )
        if other is not None:
            raise RuntimeError(
                "This workspace already has a different active Facebook Page mapping"
            )
        channel = WhatsAppChannel(
            tenant_id=tenant.id,
            provider="messenger",
            provider_account_id=page_id,
            provider_channel_id=page_id,
            twilio_account_sid=None,
            sender_address=page_id,
            is_active=True,
        )
        database_session.add(channel)
        database_session.flush()
        return channel, True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Map the configured Facebook Page ID to an existing workspace"
    )
    parser.add_argument("--workspace-slug", required=True)
    args = parser.parse_args()
    channel, created = configure_page(args.workspace_slug)
    action = "created" if created else "reused"
    print(f"Meta Messenger Page mapping {action} (channel_id={channel.id})")


if __name__ == "__main__":
    main()
