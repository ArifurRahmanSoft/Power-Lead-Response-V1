import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.utils.identifiers import normalize_email, normalize_login_id


def get_user_by_email(
    database_session: Session,
    normalized_email: str,
) -> User | None:
    return database_session.scalar(
        select(User).where(User.email == normalized_email)
    )


def get_user_by_login_id(
    database_session: Session,
    normalized_login_id: str,
) -> User | None:
    return database_session.scalar(
        select(User).where(User.login_id == normalized_login_id)
    )


def get_user_by_identifier(
    database_session: Session,
    identifier: str,
) -> User | None:
    if "@" in identifier:
        return get_user_by_email(database_session, normalize_email(identifier))
    return get_user_by_login_id(
        database_session,
        normalize_login_id(identifier),
    )


def get_user_by_id(
    database_session: Session,
    user_id: uuid.UUID,
) -> User | None:
    return database_session.get(User, user_id)


def get_user_by_id_for_update(
    database_session: Session,
    user_id: uuid.UUID,
) -> User | None:
    return database_session.scalar(
        select(User).where(User.id == user_id).with_for_update()
    )
