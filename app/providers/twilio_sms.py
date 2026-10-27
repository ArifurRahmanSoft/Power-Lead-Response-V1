from dataclasses import dataclass
from datetime import datetime

from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from app.core.config import Settings, settings


class TwilioSmsError(Exception):
    code = "twilio_error"
    safe_message = "Twilio SMS operation failed"

    def __init__(
        self,
        *,
        code: str | None = None,
        safe_message: str | None = None,
        http_status: int | None = None,
    ):
        self.code = code or self.code
        self.safe_message = safe_message or self.safe_message
        self.http_status = (
            http_status
            if isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and 100 <= http_status <= 599
            else None
        )
        super().__init__(self.safe_message)


class TwilioSmsConfigurationError(TwilioSmsError):
    code = "twilio_not_configured"
    safe_message = "Twilio SMS is not configured"


class TwilioSmsRejectedError(TwilioSmsError):
    code = "twilio_rejected"
    safe_message = "Twilio rejected the SMS request"


class TwilioSmsPreflightError(TwilioSmsError):
    code = "twilio_preflight_failed"
    safe_message = "The SMS request failed validation before provider acceptance"


class TwilioSmsProviderFailureError(TwilioSmsError):
    code = "twilio_provider_failure"
    safe_message = "The SMS provider request failed before acceptance was confirmed"


class TwilioSmsAcceptanceUnknownError(TwilioSmsError):
    code = "twilio_acceptance_unknown"
    safe_message = "Twilio SMS acceptance could not be determined"


@dataclass(frozen=True)
class TwilioSmsResult:
    sid: str
    status: str
    error_code: str | None = None
    date_created: datetime | None = None
    date_sent: datetime | None = None


def normalize_twilio_status(value: object) -> str:
    status = str(value or "unknown").strip().casefold()
    if status in {
        "accepted", "queued", "sent", "delivered", "failed", "undelivered"
    }:
        return status
    if status in {"sending", "scheduled"}:
        return "queued"
    if status in {"canceled", "cancelled"}:
        return "failed"
    return "unknown"


def safe_twilio_rejection_message(code: object) -> str:
    normalized = str(code) if code is not None else ""
    if normalized == "20003":
        return "Twilio authentication failed; verify the configured Account SID and auth token"
    if normalized == "20429":
        return "Twilio rate-limited the SMS request; wait before trying a new send"
    if normalized in {"21211", "21614"}:
        return "Twilio rejected the recipient phone number; verify that it is a valid SMS-capable number in E.164 format"
    if normalized == "21608":
        return (
            "Twilio account restrictions allow SMS only to verified recipients; "
            "verify this recipient in Twilio or complete the required account upgrade "
            "and compliance profile"
        )
    if normalized == "21610":
        return "The recipient has opted out; obtain renewed consent and have them opt in before sending again"
    if normalized == "21606":
        return "The configured Twilio sender cannot send to this recipient; verify the sender is SMS-capable for this destination"
    if normalized == "21612":
        return (
            "Twilio cannot send using the current sender and recipient combination; "
            "verify E.164 formatting and the destination's sender-type and network "
            "restrictions"
        )
    if normalized == "21611":
        return "The Twilio sender has too many queued messages; wait for the queue to clear before trying a new send"
    if normalized == "21617":
        return "Twilio rejected the SMS because its rendered body exceeds the provider length limit"
    if normalized == "21408":
        return "Twilio Messaging Geographic Permissions block this destination; enable the supported region or correct the country code"
    if normalized in {"30007", "30008"}:
        return "The carrier or Twilio rejected the SMS content; review the message content and destination messaging rules"
    if normalized == "63016":
        return "Twilio requires an approved content template for this destination or channel; select an approved template"
    if normalized == "63038":
        return (
            "Twilio account has reached its daily messaging limit; wait for the "
            "rolling 24-hour allowance to reset or review the account messaging limit"
        )
    return (
        "Twilio rejected the SMS request; check trial recipient, geographic, "
        "sender, and content restrictions"
    )


class TwilioSmsProvider:
    def __init__(self, configuration: Settings | None = None) -> None:
        self._settings = configuration or settings
        self._account_sid = (
            self._settings.twilio_account_sid.get_secret_value()
            if self._settings.twilio_account_sid
            else ""
        )
        self._auth_token = (
            self._settings.twilio_auth_token.get_secret_value()
            if self._settings.twilio_auth_token
            else ""
        )
        self.from_number = (self._settings.twilio_sms_from or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._account_sid and self._auth_token and self.from_number)

    def _client(self) -> Client:
        if not self.configured:
            raise TwilioSmsConfigurationError
        http_client = TwilioHttpClient(
            timeout=self._settings.sms_direct_send_timeout_seconds
        )
        return Client(
            self._account_sid,
            self._auth_token,
            http_client=http_client,
        )

    def send_message(self, *, to: str, body: str) -> TwilioSmsResult:
        try:
            client = self._client()
        except TwilioSmsConfigurationError:
            raise
        except Exception as exception:
            raise TwilioSmsProviderFailureError from exception
        try:
            message = client.messages.create(
                to=to,
                from_=self.from_number,
                body=body,
            )
        except TwilioRestException as exception:
            code = str(exception.code) if exception.code is not None else None
            raise TwilioSmsRejectedError(
                code=code,
                safe_message=safe_twilio_rejection_message(exception.code),
                http_status=exception.status,
            ) from exception
        except (TypeError, ValueError) as exception:
            raise TwilioSmsPreflightError from exception
        except TwilioException as exception:
            raise TwilioSmsAcceptanceUnknownError from exception
        except (OSError, TimeoutError) as exception:
            raise TwilioSmsAcceptanceUnknownError from exception
        except Exception as exception:
            # The SDK call began, so an unexpected error cannot safely prove that
            # Twilio did not accept the request.
            raise TwilioSmsAcceptanceUnknownError from exception
        try:
            sid = str(message.sid)
            if not sid:
                raise ValueError("missing provider message SID")
            return TwilioSmsResult(
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
            raise TwilioSmsAcceptanceUnknownError(
                safe_message=(
                    "Twilio responded, but its SMS acceptance response could not be recorded"
                )
            ) from exception

    def fetch_message(self, message_sid: str) -> TwilioSmsResult:
        try:
            message = self._client().messages(message_sid).fetch()
        except TwilioRestException as exception:
            code = str(exception.code) if exception.code is not None else None
            raise TwilioSmsRejectedError(
                code=code,
                safe_message="Twilio message status could not be retrieved",
                http_status=exception.status,
            ) from exception
        except TwilioSmsConfigurationError:
            raise
        except (TypeError, ValueError) as exception:
            raise TwilioSmsPreflightError(
                safe_message="Twilio message status lookup request is invalid"
            ) from exception
        except TwilioException as exception:
            raise TwilioSmsAcceptanceUnknownError(
                safe_message="Twilio message status lookup could not be completed"
            ) from exception
        except (OSError, TimeoutError) as exception:
            raise TwilioSmsAcceptanceUnknownError(
                safe_message="Twilio message status lookup could not be completed"
            ) from exception
        except Exception as exception:
            raise TwilioSmsProviderFailureError(
                safe_message="Twilio message status lookup failed"
            ) from exception
        return TwilioSmsResult(
            sid=str(message.sid),
            status=normalize_twilio_status(message.status),
            error_code=(
                str(message.error_code)
                if getattr(message, "error_code", None) is not None
                else None
            ),
            date_created=getattr(message, "date_created", None),
            date_sent=getattr(message, "date_sent", None),
        )


def get_twilio_sms_provider() -> TwilioSmsProvider:
    # Read the central environment-backed settings for each request. This avoids
    # retaining an empty provider snapshot when a local .env file is populated
    # after a development server has started.
    return TwilioSmsProvider(Settings())
