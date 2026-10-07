from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.email import EmailDeliveryError, send_password_reset_email
from app.repositories.password_reset_repository import (
    create_password_reset_token,
    get_password_reset_token_by_hash,
    invalidate_password_reset_tokens,
)
from app.repositories.user_repository import (
    get_user_by_email,
    get_user_by_id_for_update,
)
from app.schemas.password_reset import ForgotPasswordRequest, ResetPasswordRequest
from app.utils.password import hash_password
from app.utils.token_generator import (
    generate_password_reset_token,
    hash_password_reset_token,
)


class InvalidPasswordResetTokenError(Exception):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def request_password_reset(
    database_session: Session,
    payload: ForgotPasswordRequest,
) -> None:
    normalized_email = str(payload.email).strip().lower()
    user = get_user_by_email(database_session, normalized_email)

    # Generate and hash on every path so the response behavior stays uniform.
    raw_token = generate_password_reset_token()
    token_hash = hash_password_reset_token(raw_token)

    if user is None or not user.is_active:
        return

    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.password_reset_token_expire_minutes
    )
    create_password_reset_token(
        database_session,
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    database_session.commit()

    try:
        send_password_reset_email(user.email, raw_token)
    except EmailDeliveryError:
        # Delivery adapters must report failures without exposing account state.
        return


def reset_password(
    database_session: Session,
    payload: ResetPasswordRequest,
) -> None:
    token_hash = hash_password_reset_token(payload.token)
    reset_token = get_password_reset_token_by_hash(database_session, token_hash)
    if reset_token is None:
        raise InvalidPasswordResetTokenError

    user = get_user_by_id_for_update(database_session, reset_token.user_id)
    if user is None:
        database_session.rollback()
        raise InvalidPasswordResetTokenError

    reset_token = get_password_reset_token_by_hash(
        database_session,
        token_hash,
        for_update=True,
    )
    now = datetime.now(timezone.utc)
    if (
        reset_token is None
        or reset_token.used_at is not None
        or _as_utc(reset_token.expires_at) <= now
    ):
        database_session.rollback()
        raise InvalidPasswordResetTokenError

    user.password_hash = hash_password(payload.new_password)
    invalidate_password_reset_tokens(database_session, user.id, now)
    database_session.commit()
