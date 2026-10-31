import argparse

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.tenant import Tenant
from app.models.whatsapp import WhatsAppChannel


PROVIDER = "instagram"


def configure_account(workspace_slug: str) -> tuple[WhatsAppChannel, bool]:
    configuration = get_settings()
    account_id = (configuration.meta_instagram_account_id or "").strip()
    if not account_id:
        raise RuntimeError("META_INSTAGRAM_ACCOUNT_ID must be configured")
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
                WhatsAppChannel.provider == PROVIDER,
                WhatsAppChannel.provider_account_id == account_id,
                WhatsAppChannel.provider_channel_id == account_id,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.tenant_id != tenant.id:
                raise RuntimeError(
                    "The configured Instagram account is already mapped to another workspace"
                )
            existing.sender_address = account_id
            existing.is_active = True
            database_session.flush()
            return existing, False
        other = database_session.scalar(
            select(WhatsAppChannel).where(
                WhatsAppChannel.tenant_id == tenant.id,
                WhatsAppChannel.provider == PROVIDER,
                WhatsAppChannel.is_active.is_(True),
            )
        )
        if other is not None:
            raise RuntimeError(
                "This workspace already has a different active Instagram account mapping"
            )
        channel = WhatsAppChannel(
            tenant_id=tenant.id,
            provider=PROVIDER,
            provider_account_id=account_id,
            provider_channel_id=account_id,
            twilio_account_sid=None,
            sender_address=account_id,
            is_active=True,
        )
        database_session.add(channel)
        database_session.flush()
        return channel, True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Map the configured Instagram professional account to a workspace"
    )
    parser.add_argument("--workspace-slug", required=True)
    args = parser.parse_args()
    channel, created = configure_account(args.workspace_slug)
    action = "created" if created else "reused"
    print(f"Meta Instagram account mapping {action} (channel_id={channel.id})")


if __name__ == "__main__":
    main()
