import argparse

from sqlalchemy import select

from app.core.config import Settings
from app.core.database import SessionLocal
from app.models.tenant import Tenant
from app.models.whatsapp import WhatsAppChannel
from app.providers.twilio_whatsapp import normalize_whatsapp_address


def configure_channel(workspace_slug: str) -> WhatsAppChannel:
    settings = Settings()
    account_sid = (
        settings.twilio_account_sid.get_secret_value()
        if settings.twilio_account_sid
        else ""
    )
    if not account_sid or not settings.twilio_whatsapp_from:
        raise RuntimeError(
            "TWILIO_ACCOUNT_SID and TWILIO_WHATSAPP_FROM must be configured"
        )
    sender = normalize_whatsapp_address(settings.twilio_whatsapp_from)
    with SessionLocal() as database_session:
        tenant = database_session.scalar(
            select(Tenant).where(Tenant.slug == workspace_slug, Tenant.is_active.is_(True))
        )
        if tenant is None:
            raise RuntimeError("No active workspace exists with that exact slug")
        existing = database_session.scalar(
            select(WhatsAppChannel).where(
                WhatsAppChannel.twilio_account_sid == account_sid,
                WhatsAppChannel.sender_address == sender,
            )
        )
        if existing is not None:
            if existing.tenant_id != tenant.id:
                raise RuntimeError(
                    "The configured Twilio WhatsApp sender is already mapped to another workspace"
                )
            existing.is_active = True
            existing.provider = "twilio"
            existing.provider_account_id = account_sid
            existing.provider_channel_id = sender
            database_session.commit()
            database_session.refresh(existing)
            return existing
        channel = WhatsAppChannel(
            tenant_id=tenant.id,
            provider="twilio",
            provider_account_id=account_sid,
            provider_channel_id=sender,
            twilio_account_sid=account_sid,
            sender_address=sender,
            is_active=True,
        )
        database_session.add(channel)
        database_session.commit()
        database_session.refresh(channel)
        return channel


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Map the configured Twilio WhatsApp Sandbox sender to a workspace"
    )
    parser.add_argument("--workspace-slug", required=True)
    args = parser.parse_args()
    channel = configure_channel(args.workspace_slug)
    print(f"WhatsApp channel mapping is active (channel_id={channel.id})")


if __name__ == "__main__":
    main()
