import argparse

from sqlalchemy import select

from app.core.config import Settings
from app.core.database import SessionLocal
from app.models.tenant import Tenant
from app.models.whatsapp import WhatsAppChannel
from app.utils.phone import normalize_e164_phone


def configure_channel(workspace_slug: str, sender_address: str) -> tuple[WhatsAppChannel, bool]:
    settings = Settings()
    waba_id = (settings.meta_whatsapp_waba_id or "").strip()
    phone_number_id = (settings.meta_whatsapp_phone_number_id or "").strip()
    if not waba_id or not phone_number_id:
        raise RuntimeError(
            "META_WHATSAPP_WABA_ID and META_WHATSAPP_PHONE_NUMBER_ID must be configured"
        )
    sender = normalize_e164_phone(sender_address)
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
                WhatsAppChannel.provider == "meta",
                WhatsAppChannel.provider_account_id == waba_id,
                WhatsAppChannel.provider_channel_id == phone_number_id,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.tenant_id != tenant.id:
                raise RuntimeError(
                    "The configured Meta WABA/Phone Number ID is already mapped to another workspace"
                )
            existing.sender_address = sender
            existing.is_active = True
            database_session.flush()
            return existing, False
        other = database_session.scalar(
            select(WhatsAppChannel).where(
                WhatsAppChannel.tenant_id == tenant.id,
                WhatsAppChannel.provider == "meta",
                WhatsAppChannel.is_active.is_(True),
            )
        )
        if other is not None:
            raise RuntimeError(
                "This workspace already has a different active Meta WhatsApp channel"
            )
        channel = WhatsAppChannel(
            tenant_id=tenant.id,
            provider="meta",
            provider_account_id=waba_id,
            provider_channel_id=phone_number_id,
            twilio_account_sid=None,
            sender_address=sender,
            is_active=True,
        )
        database_session.add(channel)
        database_session.flush()
        return channel, True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Map the configured Meta WABA and Phone Number ID to a workspace"
    )
    parser.add_argument("--workspace-slug", required=True)
    parser.add_argument(
        "--sender-address",
        required=True,
        help="Meta WhatsApp sender in E.164 format, for example +14155552671",
    )
    args = parser.parse_args()
    channel, created = configure_channel(args.workspace_slug, args.sender_address)
    action = "created" if created else "reused"
    print(f"Meta WhatsApp channel mapping {action} (channel_id={channel.id})")


if __name__ == "__main__":
    main()
