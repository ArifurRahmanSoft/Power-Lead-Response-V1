from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import jwt
from jwt import InvalidTokenError

from app.core.config import settings


class AccessTokenError(Exception):
    """Raised when an access token is missing required claims or is invalid."""


def create_access_token(
    user_id: UUID,
    user_email: str,
    *,
    scope: str = "onboarding",
    tenant_id: UUID | None = None,
    membership_id: UUID | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    expires_at = datetime.now(timezone.utc) + (
        expires_delta
        or timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload = {
        "sub": str(user_id),
        "email": user_email,
        "scope": scope,
        "exp": expires_at,
    }
    if tenant_id is not None:
        payload["tenant_id"] = str(tenant_id)
    if membership_id is not None:
        payload["membership_id"] = str(membership_id)
    return jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "email", "exp"]},
        )
    except InvalidTokenError as exception:
        raise AccessTokenError from exception
