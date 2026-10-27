from pathlib import Path

from cryptography.fernet import Fernet
from pytest import MonkeyPatch

from app.core.config import Settings


def _required_environment(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv(
        "LOCAL_DATABASE_URL",
        "postgresql://user:password@localhost:5432/local_database",
    )
    monkeypatch.setenv(
        "PRODUCTION_DATABASE_URL",
        "postgresql://user:password@database.example.com:5432/production_database",
    )
    monkeypatch.setenv(
        "JWT_SECRET_KEY",
        "test-only-jwt-secret-key-that-is-at-least-32-characters",
    )


def test_render_process_environment_loads_smtp_encryption_key(
    monkeypatch: MonkeyPatch,
) -> None:
    _required_environment(monkeypatch)
    expected_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("SMTP_CREDENTIAL_ENCRYPTION_KEY", expected_key)

    loaded = Settings(_env_file=None)

    assert loaded.smtp_credential_encryption_key is not None
    assert loaded.smtp_credential_encryption_key.get_secret_value() == expected_key


def test_process_environment_overrides_dotenv(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _required_environment(monkeypatch)
    expected_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("SMTP_CREDENTIAL_ENCRYPTION_KEY", expected_key)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "SMTP_CREDENTIAL_ENCRYPTION_KEY=invalid-dotenv-placeholder\n",
        encoding="utf-8",
    )

    loaded = Settings(_env_file=dotenv)

    assert loaded.smtp_credential_encryption_key is not None
    assert loaded.smtp_credential_encryption_key.get_secret_value() == expected_key
