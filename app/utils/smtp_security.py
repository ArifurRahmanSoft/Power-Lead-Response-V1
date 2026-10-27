import ipaddress
import re
import socket


class UnsafeSmtpDestinationError(ValueError):
    pass


SMTP_PORTS = frozenset({25, 465, 587, 2525})
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "instance-data.ec2.internal",
    }
)


def normalize_smtp_host(value: str) -> str:
    host = value.strip().rstrip(".").lower()
    if not host or len(host) > 253:
        raise UnsafeSmtpDestinationError("SMTP host is invalid")
    if any(character.isspace() for character in host) or any(
        marker in host for marker in ("://", "/", "@", "\\")
    ):
        raise UnsafeSmtpDestinationError("SMTP host must be a hostname only")
    if host in _BLOCKED_HOSTS or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeSmtpDestinationError("SMTP host is not permitted")

    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exception:
            raise UnsafeSmtpDestinationError("SMTP host is invalid") from exception
        if len(ascii_host.split(".")) < 2 or not all(
            _HOST_LABEL.fullmatch(label) for label in ascii_host.split(".")
        ):
            raise UnsafeSmtpDestinationError("SMTP host is invalid")
        return ascii_host

    if not address.is_global:
        raise UnsafeSmtpDestinationError("SMTP host resolves to a non-public address")
    return address.compressed


def validate_smtp_port(port: int) -> int:
    if port not in SMTP_PORTS:
        raise UnsafeSmtpDestinationError(
            "SMTP port must be one of 25, 465, 587, or 2525"
        )
    return port


def resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve once for a pinned connection and reject every non-public result."""
    try:
        results = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exception:
        raise UnsafeSmtpDestinationError("SMTP host could not be resolved") from exception

    addresses = tuple(dict.fromkeys(result[4][0] for result in results))
    if not addresses:
        raise UnsafeSmtpDestinationError("SMTP host could not be resolved")
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exception:
            raise UnsafeSmtpDestinationError("SMTP host resolution was invalid") from exception
        if not address.is_global:
            raise UnsafeSmtpDestinationError(
                "SMTP host resolves to a non-public address"
            )
    return addresses


def verify_pinned_peer(
    peer_address: str,
    selected_address: str,
    fresh_addresses: tuple[str, ...],
) -> None:
    peer = ipaddress.ip_address(peer_address)
    if not peer.is_global or peer != ipaddress.ip_address(selected_address):
        raise UnsafeSmtpDestinationError("SMTP connection destination changed")
    if selected_address not in fresh_addresses:
        raise UnsafeSmtpDestinationError("SMTP host resolution changed during connection")
