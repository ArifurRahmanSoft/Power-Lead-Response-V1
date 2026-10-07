from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.password_reset_token import PasswordResetToken
from app.models.user import User
from app.utils.password import hash_password, verify_password
from app.utils.token_generator import hash_password_reset_token

FORGOT_PASSWORD_URL = "/api/auth/forgot-password"
RESET_PASSWORD_URL = "/api/auth/reset-password"
USER_EMAIL = "user@gmail.com"
OLD_PASSWORD = "old-password"
NEW_PASSWORD = "new-password"
FORGOT_PASSWORD_RESPONSE = {
    "success": True,
    "message": "If the email exists, a password reset link has been sent.",
}


def create_user(database_session: Session) -> User:
    user = User(
        name="John Doe",
        email=USER_EMAIL,
        password_hash=hash_password(OLD_PASSWORD),
    )
    database_session.add(user)
    database_session.commit()
    database_session.refresh(user)
    return user


def create_reset_token(
    database_session: Session,
    user: User,
    raw_token: str,
    *,
    expires_at: datetime | None = None,
    used_at: datetime | None = None,
) -> PasswordResetToken:
    reset_token = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_password_reset_token(raw_token),
        expires_at=expires_at
        or datetime.now(timezone.utc) + timedelta(minutes=15),
        used_at=used_at,
    )
    database_session.add(reset_token)
    database_session.commit()
    database_session.refresh(reset_token)
    return reset_token


def test_forgot_password_existing_email(
    client: TestClient,
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = create_user(database_session)
    sent_messages: list[tuple[str, str]] = []

    def capture_email(recipient_email: str, reset_token: str) -> None:
        sent_messages.append((recipient_email, reset_token))

    monkeypatch.setattr(
        "app.services.password_reset_service.send_password_reset_email",
        capture_email,
    )

    response = client.post(
        FORGOT_PASSWORD_URL,
        json={"email": "  USER@GMAIL.COM  "},
    )

    assert response.status_code == 200
    assert response.json() == FORGOT_PASSWORD_RESPONSE
    assert len(sent_messages) == 1
    recipient_email, raw_token = sent_messages[0]
    assert recipient_email == USER_EMAIL

    reset_token = database_session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
    )
    assert reset_token is not None
    assert reset_token.token_hash == hash_password_reset_token(raw_token)
    assert raw_token != reset_token.token_hash
    assert reset_token.used_at is None


def test_forgot_password_non_existing_email(
    client: TestClient,
    database_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email_was_sent = False

    def capture_email(recipient_email: str, reset_token: str) -> None:
        del recipient_email, reset_token
        nonlocal email_was_sent
        email_was_sent = True

    monkeypatch.setattr(
        "app.services.password_reset_service.send_password_reset_email",
        capture_email,
    )

    response = client.post(
        FORGOT_PASSWORD_URL,
        json={"email": "unknown@gmail.com"},
    )

    assert response.status_code == 200
    assert response.json() == FORGOT_PASSWORD_RESPONSE
    assert email_was_sent is False
    assert database_session.scalar(select(PasswordResetToken)) is None


def test_forgot_password_invalid_email(client: TestClient) -> None:
    response = client.post(
        FORGOT_PASSWORD_URL,
        json={"email": "not-an-email"},
    )

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert any(
        error["field"] == "email" for error in response.json()["errors"]
    )


def test_reset_password_with_valid_token(
    client: TestClient,
    database_session: Session,
) -> None:
    user = create_user(database_session)
    raw_token = "valid-reset-token"
    current_token = create_reset_token(database_session, user, raw_token)
    previous_token = create_reset_token(
        database_session,
        user,
        "previous-reset-token",
    )

    response = client.post(
        RESET_PASSWORD_URL,
        json={
            "token": raw_token,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "Password reset successful",
    }
    database_session.expire_all()
    assert database_session.get(PasswordResetToken, current_token.id).used_at is not None
    assert database_session.get(PasswordResetToken, previous_token.id).used_at is not None


def test_reset_password_with_expired_token(
    client: TestClient,
    database_session: Session,
) -> None:
    user = create_user(database_session)
    raw_token = "expired-reset-token"
    create_reset_token(
        database_session,
        user,
        raw_token,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    response = client.post(
        RESET_PASSWORD_URL,
        json={
            "token": raw_token,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "message": "Invalid or expired password reset token",
    }


def test_reset_password_with_used_token(
    client: TestClient,
    database_session: Session,
) -> None:
    user = create_user(database_session)
    raw_token = "used-reset-token"
    create_reset_token(
        database_session,
        user,
        raw_token,
        used_at=datetime.now(timezone.utc),
    )

    response = client.post(
        RESET_PASSWORD_URL,
        json={
            "token": raw_token,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )

    assert response.status_code == 400
    assert response.json()["message"] == "Invalid or expired password reset token"


def test_reset_password_mismatch(client: TestClient) -> None:
    response = client.post(
        RESET_PASSWORD_URL,
        json={
            "token": "some-reset-token",
            "new_password": NEW_PASSWORD,
            "confirm_password": "different-password",
        },
    )

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert "must match" in response.text
    assert NEW_PASSWORD not in response.text


def test_successful_password_update(
    client: TestClient,
    database_session: Session,
) -> None:
    user = create_user(database_session)
    raw_token = "password-update-token"
    create_reset_token(database_session, user, raw_token)

    response = client.post(
        RESET_PASSWORD_URL,
        json={
            "token": raw_token,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )

    assert response.status_code == 200
    database_session.expire_all()
    updated_user = database_session.get(User, user.id)
    assert updated_user is not None
    assert verify_password(NEW_PASSWORD, updated_user.password_hash)
    assert not verify_password(OLD_PASSWORD, updated_user.password_hash)
