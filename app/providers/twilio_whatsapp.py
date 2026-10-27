from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app.core.config import Settings
from app.providers.twilio_sms import normalize_twilio_status
from app.utils.phone import normalize_e164_phone


class TwilioWhatsAppError(Exception):
    code = "twilio_whatsapp_error"
    safe_message = "Twilio WhatsApp operation failed"

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


class TwilioWhatsAppConfigurationError(TwilioWhatsAppError):
    code = "whatsapp_not_configured"
    safe_message = "Twilio WhatsApp is not configured"


class TwilioWhatsAppRejectedError(TwilioWhatsAppError):
    code = "twilio_rejected"
    safe_message = "Twilio rejected the WhatsApp message"


class TwilioWhatsAppAcceptanceUnknownError(TwilioWhatsAppError):
    code = "acceptance_unknown"
    safe_message = "Twilio WhatsApp acceptance could not be determined"


@dataclass(frozen=True)
class TwilioWhatsAppResult:
    sid: str
    status: str
    error_code: str | None = None
    date_created: datetime | None = None
    date_sent: datetime | None = None


def normalize_whatsapp_address(value: object) -> str:
    address = str(value or "").strip()
    if address.casefold().startswith("whatsapp:"):
        address = address.split(":", 1)[1]
    return f"whatsapp:{normalize_e164_phone(address)}"


def safe_twilio_whatsapp_rejection_message(code: object) -> str:
    normalized = str(code or "")
    if normalized == "63016":
        return (
            "An approved WhatsApp template is required outside the customer-service "
            "window"
        )
    if normalized == "21610":
        return "The recipient has opted out of messages from this sender"
    if normalized == "21408":
        return "Twilio geographic permissions block this WhatsApp destination"
    if normalized == "21612":
        return "Twilio cannot use the configured WhatsApp sender for this recipient"
    if normalized == "20003":
        return "Twilio authentication failed"
    if normalized == "20429":
        return "Twilio rate-limited the WhatsApp request"
    return "Twilio rejected the WhatsApp message; review the provider error code"


def externally_configured_url(request_url: str, configured_base_url: str | None) -> str:
    if not configured_base_url:
        return request_url
    parsed = urlsplit(request_url)
    base = urlsplit(configured_base_url)
    return urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, ""))


class TwilioWhatsAppProvider:
    def __init__(self, configuration: Settings | None = None) -> None:
        config = configuration or Settings()
        self.account_sid = (
            config.twilio_account_sid.get_secret_value()
            if config.twilio_account_sid
            else ""
        )
        self._auth_token = (
            config.twilio_auth_token.get_secret_value()
            if config.twilio_auth_token
            else ""
        )
        raw_sender = (config.twilio_whatsapp_from or "").strip()
        try:
            self.from_address = normalize_whatsapp_address(raw_sender) if raw_sender else ""
        except ValueError:
            self.from_address = ""
        self.webhook_base_url = config.whatsapp_webhook_base_url
        self.timeout_seconds = config.whatsapp_reply_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self._auth_token and self.from_address)

    @property
    def status_callback_url(self) -> str | None:
        if not self.webhook_base_url:
            return None
        return f"{self.webhook_base_url}/api/webhooks/twilio/whatsapp/status"

    def validate_signature(
        self,
        *,
        request_url: str,
        form_values: object,
        signature: str,
    ) -> bool:
        if not self._auth_token or not signature:
            return False
        external_url = externally_configured_url(request_url, self.webhook_base_url)
        return RequestValidator(self._auth_token).validate(
            external_url,
            form_values,
            signature,
        )

    def _client(self) -> Client:
        if not self.configured:
            raise TwilioWhatsAppConfigurationError
        return Client(
            self.account_sid,
            self._auth_token,
            http_client=TwilioHttpClient(timeout=self.timeout_seconds),
        )

    def send_text(self, *, to: str, body: str) -> TwilioWhatsAppResult:
        callback_url = self.status_callback_url
        if not callback_url:
            raise TwilioWhatsAppConfigurationError(
                code="whatsapp_callback_url_missing",
                safe_message="The public WhatsApp webhook base URL is not configured",
            )
        try:
            message = self._client().messages.create(
                to=normalize_whatsapp_address(to),
                from_=self.from_address,
                body=body,
                status_callback=callback_url,
            )
        except TwilioWhatsAppConfigurationError:
            raise
        except TwilioRestException as exception:
            code = str(exception.code) if exception.code is not None else None
            raise TwilioWhatsAppRejectedError(
                code=code,
                safe_message=safe_twilio_whatsapp_rejection_message(code),
                http_status=exception.status,
            ) from exception
        except (TypeError, ValueError) as exception:
            raise TwilioWhatsAppRejectedError(
                code="invalid_provider_request",
                safe_message="The WhatsApp provider request is invalid",
            ) from exception
        except (TwilioException, OSError, TimeoutError) as exception:
            raise TwilioWhatsAppAcceptanceUnknownError from exception
        except Exception as exception:
            raise TwilioWhatsAppAcceptanceUnknownError from exception

        try:
            sid = str(message.sid)
            if not sid:
                raise ValueError("missing provider message SID")
            return TwilioWhatsAppResult(
                sid=sid,
                status=normalize_twilio_status(message.status),
                error_code=(
                    str(message.error_code)
                    if getattr(message, "error_code", None) is not None
                    else None
                ),
                date_created=getattr(message, "date_created", None),
                date_sent=getattr(message, "date_sent", None),
            )
        except Exception as exception:
            raise TwilioWhatsAppAcceptanceUnknownError(
                safe_message=(
                    "Twilio responded, but its WhatsApp acceptance response could not "
                    "be recorded"
                )
            ) from exception


def get_twilio_whatsapp_provider() -> TwilioWhatsAppProvider:
    return TwilioWhatsAppProvider(Settings())
