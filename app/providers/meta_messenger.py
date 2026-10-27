import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.core.meta_webhook import validate_meta_challenge, validate_meta_signature


META_OBJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
GRAPH_VERSION_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+$")


class MetaMessengerError(Exception):
    code = "meta_messenger_error"
    safe_message = "Meta Messenger operation failed"

    def __init__(self, *, code: str | None = None, safe_message: str | None = None,
                 http_status: int | None = None) -> None:
        self.code = code or self.code
        self.safe_message = safe_message or self.safe_message
        self.http_status = http_status
        super().__init__(self.safe_message)


class MetaMessengerConfigurationError(MetaMessengerError):
    code = "meta_messenger_not_configured"
    safe_message = "Meta Messenger Send API is not configured"


class MetaMessengerRejectedError(MetaMessengerError):
    code = "meta_rejected"
    safe_message = "Meta rejected the Messenger reply"


class MetaMessengerAcceptanceUnknownError(MetaMessengerError):
    code = "acceptance_unknown"
    safe_message = "Meta Messenger acceptance could not be determined"


class MetaMessengerProfileUnavailableError(MetaMessengerError):
    code = "profile_unavailable"
    safe_message = "Meta Messenger profile information is unavailable"


@dataclass(frozen=True)
class MetaMessengerResult:
    message_id: str
    recipient_id: str | None = None
    status: str = "accepted"


@dataclass(frozen=True)
class MetaMessengerPageProfile:
    display_name: str


@dataclass(frozen=True)
class MetaMessengerUserProfile:
    display_name: str


def safe_messenger_error(code: object) -> str:
    normalized = str(code or "meta_rejected")
    known = {
        "4": "Meta rate-limited the Messenger request",
        "10": "The Page token lacks permission to send Messenger replies",
        "17": "Meta rate-limited the Messenger request",
        "32": "Meta rate-limited the Messenger request",
        "100": "The Messenger Page or recipient is unavailable",
        "190": "The Messenger Page access token is invalid or expired",
        "200": "The Page token lacks permission to send Messenger replies",
        "551": "The Messenger recipient is unavailable",
        "613": "Meta rate-limited the Messenger request",
        "80004": "Meta rate-limited the Messenger request",
    }
    return known.get(normalized, f"Meta rejected the Messenger reply (code {normalized})")


def safe_profile_error(code: object) -> str:
    normalized = str(code or "profile_unavailable")
    known = {
        "4": "Meta rate-limited the profile request",
        "10": "Meta did not authorize access to this Messenger profile",
        "100": "The Page or Messenger profile is unavailable",
        "190": "The Messenger Page access token is invalid or expired",
        "200": "Meta did not authorize access to this Messenger profile",
        "613": "Meta rate-limited the profile request",
        "2018247": "Meta did not grant access to the requested profile fields",
        "80004": "Meta rate-limited the profile request",
    }
    return known.get(normalized, f"Meta profile information is unavailable (code {normalized})")


class MetaMessengerProvider:
    def __init__(self, configuration: Settings | None = None) -> None:
        config = configuration or Settings()
        self.page_id = (config.meta_messenger_page_id or "").strip()
        self.page_access_token = (
            config.meta_messenger_page_access_token.get_secret_value()
            if config.meta_messenger_page_access_token else ""
        )
        self.verify_token = (
            config.meta_messenger_verify_token.get_secret_value()
            if config.meta_messenger_verify_token else ""
        )
        self.app_secret = (
            config.meta_messenger_app_secret.get_secret_value()
            if config.meta_messenger_app_secret
            else (config.meta_app_secret.get_secret_value() if config.meta_app_secret else "")
        )
        version = (getattr(config, "meta_graph_api_version", "v26.0") or "").strip()
        self.graph_api_version = version if GRAPH_VERSION_PATTERN.fullmatch(version) else ""
        self.timeout_seconds = getattr(config, "meta_messenger_timeout_seconds", 20.0)

    @property
    def webhook_configured(self) -> bool:
        return bool(META_OBJECT_ID_PATTERN.fullmatch(self.page_id)
                    and self.verify_token and self.app_secret)

    @property
    def send_api_configured(self) -> bool:
        return bool(META_OBJECT_ID_PATTERN.fullmatch(self.page_id)
                    and self.page_access_token and self.graph_api_version)

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"

    def validate_challenge(self, *, mode: str, verify_token: str) -> bool:
        return validate_meta_challenge(expected_token=self.verify_token, mode=mode,
                                       supplied_token=verify_token)

    def validate_signature(self, raw_body: bytes, signature: str) -> bool:
        return validate_meta_signature(app_secret=self.app_secret, raw_body=raw_body,
                                       signature=signature)

    def _headers(self) -> dict[str, str]:
        if not self.send_api_configured:
            raise MetaMessengerConfigurationError()
        return {"Authorization": f"Bearer {self.page_access_token}",
                "Content-Type": "application/json"}

    async def _get_profile_object(
        self,
        *,
        object_id: str,
        fields: str,
    ) -> dict[str, Any]:
        if not self.send_api_configured:
            raise MetaMessengerConfigurationError()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    f"{self.base_url}/{object_id}",
                    headers={"Authorization": f"Bearer {self.page_access_token}"},
                    params={"fields": fields},
                )
        except MetaMessengerConfigurationError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaMessengerProfileUnavailableError(
                code="meta_unavailable",
                safe_message="Meta profile information is temporarily unavailable",
            ) from exception
        except Exception as exception:
            raise MetaMessengerProfileUnavailableError(
                code="meta_unavailable",
                safe_message="Meta profile information is temporarily unavailable",
            ) from exception
        if response.status_code >= 400:
            rejected = self._provider_error(response)
            raise MetaMessengerProfileUnavailableError(
                code=rejected.code,
                safe_message=safe_profile_error(rejected.code),
                http_status=rejected.http_status,
            )
        try:
            document = response.json()
        except (TypeError, ValueError) as exception:
            raise MetaMessengerProfileUnavailableError(
                code="invalid_meta_response",
                safe_message="Meta returned invalid profile information",
            ) from exception
        if not isinstance(document, dict):
            raise MetaMessengerProfileUnavailableError(
                code="invalid_meta_response",
                safe_message="Meta returned invalid profile information",
            )
        return document

    async def get_page_profile(self) -> MetaMessengerPageProfile:
        document = await self._get_profile_object(
            object_id=self.page_id,
            fields="name",
        )
        display_name = " ".join(str(document.get("name") or "").strip().split())
        if not display_name:
            raise MetaMessengerProfileUnavailableError(
                code="page_name_unavailable",
                safe_message="The configured Page name is unavailable",
            )
        return MetaMessengerPageProfile(display_name=display_name[:200])

    async def get_user_profile(self, *, psid: str) -> MetaMessengerUserProfile:
        if not META_OBJECT_ID_PATTERN.fullmatch(psid):
            raise MetaMessengerProfileUnavailableError(
                code="invalid_psid",
                safe_message="The Messenger contact identifier is invalid",
            )
        document = await self._get_profile_object(
            object_id=psid,
            fields="first_name,last_name",
        )
        display_name = " ".join(
            part
            for part in (
                str(document.get("first_name") or "").strip(),
                str(document.get("last_name") or "").strip(),
            )
            if part
        )
        display_name = " ".join(display_name.split())
        if not display_name:
            raise MetaMessengerProfileUnavailableError(
                code="profile_name_unavailable",
                safe_message="The Messenger contact name is unavailable",
            )
        return MetaMessengerUserProfile(display_name=display_name[:200])

    @staticmethod
    def _provider_error(response: httpx.Response) -> MetaMessengerRejectedError:
        code: str | None = None
        try:
            error = response.json().get("error", {})
            code = str(error.get("code")) if error.get("code") is not None else None
        except (TypeError, ValueError):
            pass
        normalized = code or f"http_{response.status_code}"
        return MetaMessengerRejectedError(code=normalized,
            safe_message=safe_messenger_error(normalized), http_status=response.status_code)

    async def send_text(self, *, to: str, body: str,
                        metadata: str | None = None) -> MetaMessengerResult:
        message: dict[str, Any] = {"text": body}
        if metadata:
            message["metadata"] = metadata
        payload = {"recipient": {"id": to}, "messaging_type": "RESPONSE",
                   "message": message}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(f"{self.base_url}/{self.page_id}/messages",
                                             headers=self._headers(), json=payload)
        except MetaMessengerConfigurationError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaMessengerAcceptanceUnknownError() from exception
        except Exception as exception:
            raise MetaMessengerAcceptanceUnknownError() from exception
        if response.status_code >= 400:
            raise self._provider_error(response)
        try:
            document = response.json()
            message_id = str(document["message_id"])
            recipient_id = str(document["recipient_id"]) if document.get("recipient_id") is not None else None
            if not message_id:
                raise ValueError("missing message id")
        except (KeyError, TypeError, ValueError) as exception:
            raise MetaMessengerAcceptanceUnknownError(
                safe_message="Meta responded, but its Messenger acceptance response could not be recorded"
            ) from exception
        return MetaMessengerResult(message_id=message_id, recipient_id=recipient_id)


def get_meta_messenger_provider() -> MetaMessengerProvider:
    return MetaMessengerProvider(Settings())
