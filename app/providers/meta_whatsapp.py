import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.core.meta_webhook import validate_meta_challenge, validate_meta_signature
from app.utils.phone import normalize_e164_phone


GRAPH_VERSION_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+$")


class MetaWhatsAppError(Exception):
    code = "meta_whatsapp_error"
    safe_message = "Meta WhatsApp operation failed"

    def __init__(
        self,
        *,
        code: str | None = None,
        safe_message: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code or self.code
        self.safe_message = safe_message or self.safe_message
        self.http_status = http_status
        super().__init__(self.safe_message)


class MetaWhatsAppConfigurationError(MetaWhatsAppError):
    code = "meta_whatsapp_not_configured"
    safe_message = "Meta WhatsApp Cloud API is not configured"


class MetaWhatsAppRejectedError(MetaWhatsAppError):
    code = "meta_rejected"
    safe_message = "Meta rejected the WhatsApp request"


class MetaWhatsAppAcceptanceUnknownError(MetaWhatsAppError):
    code = "acceptance_unknown"
    safe_message = "Meta WhatsApp acceptance could not be determined"


@dataclass(frozen=True)
class MetaWhatsAppResult:
    message_id: str
    status: str = "accepted"


def normalize_meta_recipient(value: object) -> str:
    """Return Meta's digits-only international recipient format."""
    return normalize_e164_phone(value).lstrip("+")


def safe_meta_error(code: object) -> str:
    normalized = str(code or "meta_rejected")
    known = {
        "131026": "Meta could not deliver to this WhatsApp recipient",
        "131030": "The recipient is not in the permitted test-recipient list",
        "131047": "An approved WhatsApp template is required outside the service window",
        "131049": "Meta did not deliver this marketing message to preserve ecosystem quality",
        "132000": "The approved template parameters do not match the template definition",
        "132001": "The selected Meta WhatsApp template is unavailable for this language",
        "190": "Meta access-token authentication failed",
        "4": "Meta rate-limited the WhatsApp request",
        "80007": "Meta rate-limited the WhatsApp request",
    }
    return known.get(normalized, f"Meta rejected the WhatsApp request (code {normalized})")


class MetaWhatsAppProvider:
    def __init__(self, configuration: Settings | None = None) -> None:
        config = configuration or Settings()
        self.access_token = (
            config.meta_whatsapp_access_token.get_secret_value()
            if config.meta_whatsapp_access_token
            else ""
        )
        self.phone_number_id = (config.meta_whatsapp_phone_number_id or "").strip()
        self.waba_id = (config.meta_whatsapp_waba_id or "").strip()
        self.app_secret = (
            config.meta_app_secret.get_secret_value() if config.meta_app_secret else ""
        )
        self.verify_token = (
            config.meta_webhook_verify_token.get_secret_value()
            if config.meta_webhook_verify_token
            else ""
        )
        version = (config.meta_graph_api_version or "").strip()
        self.graph_api_version = version if GRAPH_VERSION_PATTERN.fullmatch(version) else ""
        self.timeout_seconds = config.meta_whatsapp_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(
            self.access_token
            and self.phone_number_id
            and self.waba_id
            and self.graph_api_version
        )

    @property
    def webhook_configured(self) -> bool:
        return bool(self.app_secret and self.verify_token)

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"

    def validate_challenge(self, *, mode: str, verify_token: str) -> bool:
        return validate_meta_challenge(
            expected_token=self.verify_token,
            mode=mode,
            supplied_token=verify_token,
        )

    def validate_signature(self, raw_body: bytes, signature: str) -> bool:
        return validate_meta_signature(
            app_secret=self.app_secret,
            raw_body=raw_body,
            signature=signature,
        )

    def _headers(self) -> dict[str, str]:
        if not self.configured:
            raise MetaWhatsAppConfigurationError()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _provider_error(response: httpx.Response) -> MetaWhatsAppRejectedError:
        code: str | None = None
        try:
            error = response.json().get("error", {})
            code = str(error.get("code")) if error.get("code") is not None else None
        except (TypeError, ValueError):
            code = None
        return MetaWhatsAppRejectedError(
            code=code or f"http_{response.status_code}",
            safe_message=safe_meta_error(code or f"http_{response.status_code}"),
            http_status=response.status_code,
        )

    async def _send(self, payload: dict[str, Any]) -> MetaWhatsAppResult:
        url = f"{self.base_url}/{self.phone_number_id}/messages"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, headers=self._headers(), json=payload)
        except MetaWhatsAppConfigurationError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaWhatsAppAcceptanceUnknownError() from exception
        except Exception as exception:
            raise MetaWhatsAppAcceptanceUnknownError() from exception
        if response.status_code >= 400:
            raise self._provider_error(response)
        try:
            message_id = str(response.json()["messages"][0]["id"])
            if not message_id:
                raise ValueError("missing message id")
        except (KeyError, IndexError, TypeError, ValueError) as exception:
            raise MetaWhatsAppAcceptanceUnknownError(
                safe_message="Meta responded, but its WhatsApp acceptance response could not be recorded"
            ) from exception
        return MetaWhatsAppResult(message_id=message_id)

    async def send_text(self, *, to: str, body: str) -> MetaWhatsAppResult:
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": normalize_meta_recipient(to),
                "type": "text",
                "text": {"preview_url": False, "body": body},
            }
        )

    async def send_template(
        self,
        *,
        to: str,
        name: str,
        language: str,
        components: list[dict[str, Any]],
    ) -> MetaWhatsAppResult:
        template: dict[str, Any] = {
            "name": name,
            "language": {"code": language},
        }
        if components:
            template["components"] = components
        return await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": normalize_meta_recipient(to),
                "type": "template",
                "template": template,
            }
        )

    async def list_templates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        url = f"{self.base_url}/{self.waba_id}/message_templates"
        bounded_limit = max(1, min(limit, 2000))
        output: list[dict[str, Any]] = []
        after: str | None = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                while len(output) < bounded_limit:
                    params = {
                        "fields": "id,name,status,language,category,components",
                        "limit": str(min(100, bounded_limit - len(output))),
                    }
                    if after:
                        params["after"] = after
                    response = await client.get(
                        url, headers=self._headers(), params=params
                    )
                    if response.status_code >= 400:
                        raise self._provider_error(response)
                    document = response.json()
                    data = document.get("data", [])
                    if not isinstance(data, list):
                        raise TypeError("template data must be a list")
                    output.extend(item for item in data if isinstance(item, dict))
                    cursors = (document.get("paging") or {}).get("cursors") or {}
                    next_after = cursors.get("after")
                    if not next_after or not data or next_after == after:
                        break
                    after = str(next_after)
        except (MetaWhatsAppConfigurationError, MetaWhatsAppRejectedError):
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exception:
            raise MetaWhatsAppError(
                code="meta_unavailable",
                safe_message="Meta WhatsApp template information is temporarily unavailable",
            ) from exception
        except (TypeError, ValueError) as exception:
            raise MetaWhatsAppError(
                code="invalid_meta_response",
                safe_message="Meta returned an invalid template response",
            ) from exception
        return output[:bounded_limit]


def get_meta_whatsapp_provider() -> MetaWhatsAppProvider:
    return MetaWhatsAppProvider(Settings())
