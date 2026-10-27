import asyncio
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
import smtplib
import socket
import ssl

from app.core.config import settings
from app.utils.smtp_security import (
    UnsafeSmtpDestinationError,
    resolve_public_addresses,
    verify_pinned_peer,
)


class SmtpProviderError(Exception):
    code = "smtp_failed"
    safe_message = "SMTP operation failed"


class SmtpAuthenticationError(SmtpProviderError):
    code = "authentication_failed"
    safe_message = "SMTP authentication failed"


class SmtpConnectionError(SmtpProviderError):
    code = "connection_failed"
    safe_message = "SMTP connection failed"


class SmtpSecurityError(SmtpProviderError):
    code = "destination_blocked"
    safe_message = "SMTP destination is not permitted"


class SmtpAmbiguousDeliveryError(SmtpProviderError):
    code = "acceptance_unknown"
    safe_message = "SMTP acceptance could not be determined"


@dataclass(frozen=True)
class SmtpDeliveryConfig:
    display_name: str
    sender_email: str
    smtp_host: str
    smtp_port: int
    encryption: str
    smtp_username: str
    credential: str
    reply_to: str | None


class _PinnedSMTP(smtplib.SMTP):
    def __init__(self, host: str, port: int, pinned_address: str, timeout: float):
        self._pinned_address = pinned_address
        super().__init__(host=host, port=port, timeout=timeout)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        del host
        return socket.create_connection((self._pinned_address, port), timeout)


class _PinnedSMTPSSL(smtplib.SMTP_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        pinned_address: str,
        timeout: float,
        context: ssl.SSLContext,
    ):
        self._pinned_address = pinned_address
        super().__init__(host=host, port=port, timeout=timeout, context=context)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        raw_socket = socket.create_connection((self._pinned_address, port), timeout)
        return self.context.wrap_socket(raw_socket, server_hostname=host)


class SmtpProvider:
    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(settings.smtp_max_concurrent_operations)

    async def test_connection(self, config: SmtpDeliveryConfig) -> None:
        async with self._semaphore:
            await asyncio.to_thread(self._execute, config, None)

    async def send_test_email(
        self,
        config: SmtpDeliveryConfig,
        recipient: str,
    ) -> None:
        message = EmailMessage()
        message["Subject"] = "PowerLead sender account test"
        message["From"] = formataddr((config.display_name, config.sender_email))
        message["To"] = recipient
        if config.reply_to:
            message["Reply-To"] = config.reply_to
        message.set_content(
            "This is a requested test email from your PowerLead sender account."
        )
        async with self._semaphore:
            await asyncio.to_thread(self._execute, config, message)

    async def send_message(
        self,
        config: SmtpDeliveryConfig,
        message: EmailMessage,
    ) -> None:
        async with self._semaphore:
            await asyncio.to_thread(self._execute, config, message)

    @staticmethod
    def _execute(
        config: SmtpDeliveryConfig,
        message: EmailMessage | None,
    ) -> None:
        try:
            addresses = resolve_public_addresses(config.smtp_host, config.smtp_port)
        except UnsafeSmtpDestinationError as exception:
            raise SmtpSecurityError from exception

        last_error: Exception | None = None
        for address in addresses:
            client: smtplib.SMTP | None = None
            send_started = False
            accepted = False
            try:
                tls_context = ssl.create_default_context()
                if config.encryption == "TLS":
                    client = _PinnedSMTPSSL(
                        config.smtp_host,
                        config.smtp_port,
                        address,
                        settings.smtp_connection_timeout_seconds,
                        tls_context,
                    )
                else:
                    client = _PinnedSMTP(
                        config.smtp_host,
                        config.smtp_port,
                        address,
                        settings.smtp_connection_timeout_seconds,
                    )

                peer_address = str(client.sock.getpeername()[0])
                fresh_addresses = resolve_public_addresses(
                    config.smtp_host,
                    config.smtp_port,
                )
                verify_pinned_peer(peer_address, address, fresh_addresses)

                if config.encryption == "STARTTLS":
                    client.ehlo()
                    client.starttls(context=tls_context)
                    client.ehlo()
                client.login(config.smtp_username, config.credential)
                if message is not None:
                    send_started = True
                    client.send_message(message)
                    accepted = True
                try:
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    client.close()
                return
            except smtplib.SMTPAuthenticationError as exception:
                if client is not None:
                    client.close()
                raise SmtpAuthenticationError from exception
            except UnsafeSmtpDestinationError as exception:
                if client is not None:
                    client.close()
                raise SmtpSecurityError from exception
            except (OSError, ssl.SSLError, smtplib.SMTPException) as exception:
                if accepted:
                    return
                if send_started:
                    if client is not None:
                        client.close()
                    raise SmtpAmbiguousDeliveryError from exception
                last_error = exception
                if client is not None:
                    client.close()

        raise SmtpConnectionError from last_error


smtp_provider = SmtpProvider()


def get_smtp_provider() -> SmtpProvider:
    return smtp_provider
