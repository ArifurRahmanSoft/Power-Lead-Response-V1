import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.commands.correct_meta_instagram_account as correction_command
from app.models.tenant import Tenant
from app.models.whatsapp import WhatsAppChannel, WhatsAppConversation


OLD_ACCOUNT_ID = "9000000000001101"
NEW_ACCOUNT_ID = "9000000000002202"


def _patch_session(monkeypatch, database_session: Session) -> None:
    class TestSessionLocal:
        @staticmethod
        @contextmanager
        def begin():
            try:
                yield database_session
                database_session.commit()
            except Exception:
                database_session.rollback()
                raise

    monkeypatch.setattr(correction_command, "SessionLocal", TestSessionLocal)


def _workspace(database_session: Session, name: str = "PowerLead Response") -> Tenant:
    tenant = Tenant(name=name, slug=f"workspace-{uuid.uuid4().hex[:8]}")
    database_session.add(tenant)
    database_session.flush()
    return tenant


def _channel(
    database_session: Session,
    tenant: Tenant,
    account_id: str,
) -> WhatsAppChannel:
    channel = WhatsAppChannel(
        tenant_id=tenant.id,
        provider="instagram",
        provider_account_id=account_id,
        provider_channel_id=account_id,
        sender_address=account_id,
        is_active=True,
    )
    database_session.add(channel)
    database_session.flush()
    return channel


def test_correction_updates_empty_channel_in_place_and_is_idempotent(
    database_session: Session,
    monkeypatch,
) -> None:
    tenant = _workspace(database_session)
    channel = _channel(database_session, tenant, OLD_ACCOUNT_ID)
    original_channel_id = channel.id
    database_session.commit()
    _patch_session(monkeypatch, database_session)

    first = correction_command.correct_account(
        tenant.slug,
        OLD_ACCOUNT_ID,
        NEW_ACCOUNT_ID,
        apply=True,
    )
    second = correction_command.correct_account(
        tenant.slug,
        OLD_ACCOUNT_ID,
        NEW_ACCOUNT_ID,
        apply=True,
    )
    database_session.expire_all()

    corrected = database_session.get(WhatsAppChannel, original_channel_id)
    assert corrected is not None
    assert corrected.provider_account_id == NEW_ACCOUNT_ID
    assert corrected.provider_channel_id == NEW_ACCOUNT_ID
    assert corrected.sender_address == NEW_ACCOUNT_ID
    assert corrected.is_active is True
    assert first.action == "corrected_in_place"
    assert second.action == "already_correct"
    assert first.channel_id == second.channel_id == original_channel_id


def test_correction_preserves_historical_channel_and_creates_new_mapping(
    database_session: Session,
    monkeypatch,
) -> None:
    tenant = _workspace(database_session)
    old_channel = _channel(database_session, tenant, OLD_ACCOUNT_ID)
    database_session.add(
        WhatsAppConversation(
            tenant_id=tenant.id,
            channel_id=old_channel.id,
            external_address="18000000000000001",
            last_message_at=datetime.now(timezone.utc),
        )
    )
    database_session.commit()
    _patch_session(monkeypatch, database_session)

    result = correction_command.correct_account(
        tenant.slug,
        OLD_ACCOUNT_ID,
        NEW_ACCOUNT_ID,
        apply=True,
    )
    database_session.expire_all()

    preserved = database_session.get(WhatsAppChannel, old_channel.id)
    replacement = database_session.scalar(
        select(WhatsAppChannel).where(
            WhatsAppChannel.provider == "instagram",
            WhatsAppChannel.provider_account_id == NEW_ACCOUNT_ID,
        )
    )
    assert preserved is not None and preserved.is_active is False
    assert preserved.provider_account_id == OLD_ACCOUNT_ID
    assert replacement is not None and replacement.is_active is True
    assert replacement.id != preserved.id
    assert result.action == "created_new_mapping"
    assert result.preserved_history is True


def test_correction_refuses_cross_workspace_account_conflict_transactionally(
    database_session: Session,
    monkeypatch,
) -> None:
    intended = _workspace(database_session, "Intended")
    other = _workspace(database_session, "Other")
    old_channel = _channel(database_session, intended, OLD_ACCOUNT_ID)
    new_channel = _channel(database_session, other, NEW_ACCOUNT_ID)
    database_session.commit()
    _patch_session(monkeypatch, database_session)

    with pytest.raises(RuntimeError, match="already mapped to another workspace"):
        correction_command.correct_account(
            intended.slug,
            OLD_ACCOUNT_ID,
            NEW_ACCOUNT_ID,
            apply=True,
        )
    database_session.expire_all()

    assert database_session.get(WhatsAppChannel, old_channel.id).is_active is True
    assert database_session.get(WhatsAppChannel, new_channel.id).is_active is True


def test_correction_is_dry_run_by_default(
    database_session: Session,
    monkeypatch,
) -> None:
    tenant = _workspace(database_session)
    workspace_slug = tenant.slug
    channel = _channel(database_session, tenant, OLD_ACCOUNT_ID)
    channel_id = channel.id
    database_session.commit()
    _patch_session(monkeypatch, database_session)

    result = correction_command.correct_configured_account(
        workspace_slug,
        OLD_ACCOUNT_ID,
        configuration=SimpleNamespace(meta_instagram_account_id=NEW_ACCOUNT_ID),
    )
    database_session.expire_all()

    unchanged = database_session.get(WhatsAppChannel, channel_id)
    assert result.action == "corrected_in_place"
    assert result.applied is False
    assert unchanged is not None
    assert unchanged.provider_account_id == OLD_ACCOUNT_ID
    assert unchanged.provider_channel_id == OLD_ACCOUNT_ID
    assert unchanged.is_active is True


def test_correction_requires_configured_target_account() -> None:
    with pytest.raises(RuntimeError, match="META_INSTAGRAM_ACCOUNT_ID must be configured"):
        correction_command.correct_configured_account(
            "workspace-fixture",
            OLD_ACCOUNT_ID,
            configuration=SimpleNamespace(meta_instagram_account_id=None),
        )


def test_correction_cli_requires_explicit_scope_and_defaults_to_dry_run() -> None:
    parser = correction_command.build_parser()
    args = parser.parse_args(
        [
            "--workspace-slug",
            "workspace-fixture",
            "--old-account-id",
            OLD_ACCOUNT_ID,
        ]
    )
    applied = parser.parse_args(
        [
            "--workspace-slug",
            "workspace-fixture",
            "--old-account-id",
            OLD_ACCOUNT_ID,
            "--apply",
        ]
    )

    assert args.workspace_slug == "workspace-fixture"
    assert args.old_account_id == OLD_ACCOUNT_ID
    assert args.apply is False
    assert applied.apply is True
