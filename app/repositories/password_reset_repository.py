import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.password_reset_token import PasswordResetToken


def create_password_reset_token(
    database_session: Session,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
) -> PasswordResetToken:
    reset_token = PasswordResetToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    database_session.add(reset_token)
    return reset_token


def get_password_reset_token_by_hash(
    database_session: Session,
    token_hash: str,
    *,
    for_update: bool = False,
) -> PasswordResetToken | None:
    statement = select(PasswordResetToken).where(
        PasswordResetToken.token_hash == token_hash
    )
    if for_update:
        statement = statement.with_for_update()
    return database_session.scalar(statement)


def invalidate_password_reset_tokens(
    database_session: Session,
    user_id: uuid.UUID,
    used_at: datetime,
) -> None:
    database_session.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=used_at)
    )
