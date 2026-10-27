from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import settings


class CredentialEncryptionNotConfiguredError(RuntimeError):
    pass


class CredentialDecryptionError(RuntimeError):
    pass


def _fernet() -> MultiFernet:
    configured = settings.smtp_credential_encryption_key
    if configured is None or not configured.get_secret_value().strip():
        raise CredentialEncryptionNotConfiguredError

    raw_keys = [configured.get_secret_value().strip()]
    previous = settings.smtp_credential_previous_keys
    if previous is not None:
        raw_keys.extend(
            key.strip()
            for key in previous.get_secret_value().split(",")
            if key.strip()
        )
    try:
        return MultiFernet([Fernet(key.encode("ascii")) for key in raw_keys])
    except (ValueError, UnicodeEncodeError) as exception:
        raise CredentialEncryptionNotConfiguredError from exception


def encrypt_credential(credential: str) -> str:
    if not credential:
        raise ValueError("Credential cannot be blank")
    return _fernet().encrypt(credential.encode("utf-8")).decode("ascii")


def decrypt_credential(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exception:
        raise CredentialDecryptionError from exception
