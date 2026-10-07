from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.user import User
from app.repositories.user_repository import get_user_by_email, get_user_by_login_id
from app.schemas.user import UserRegister
from app.utils.identifiers import generate_login_id, normalize_email
from app.utils.password import hash_password


class DuplicateEmailError(Exception):
    pass


class DuplicateLoginIdError(Exception):
    pass


def register_user(database_session: Session, payload: UserRegister) -> User:
    normalized_email = normalize_email(str(payload.email))
    normalized_login_id = payload.login_id or generate_login_id()

    existing_user = get_user_by_email(database_session, normalized_email)
    if existing_user is not None:
        raise DuplicateEmailError
    if get_user_by_login_id(database_session, normalized_login_id) is not None:
        raise DuplicateLoginIdError

    user = User(
        name=payload.name.strip(),
        email=normalized_email,
        login_id=normalized_login_id,
        password_hash=hash_password(payload.password),
    )
    database_session.add(user)

    try:
        database_session.commit()
    except IntegrityError as exception:
        database_session.rollback()
        if get_user_by_login_id(database_session, normalized_login_id) is not None:
            raise DuplicateLoginIdError from exception
        raise DuplicateEmailError from exception

    database_session.refresh(user)
    return user

