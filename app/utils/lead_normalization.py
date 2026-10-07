import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

import pycountry

_SCHEME_PREFIX = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_UNSAFE_SCHEMES = frozenset({"javascript", "data", "file", "ftp"})


def normalize_optional_url(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if any(character.isspace() for character in raw):
        raise ValueError("URL cannot contain whitespace")

    scheme_match = _SCHEME_PREFIX.match(raw)
    if scheme_match:
        supplied_scheme = scheme_match.group(1).casefold()
        if supplied_scheme in _UNSAFE_SCHEMES:
            raise ValueError("URL scheme must be http or https")
        if supplied_scheme in {"http", "https"}:
            if not raw.casefold().startswith(("http://", "https://")):
                raise ValueError("URL must use http:// or https://")
            candidate = raw
        elif "." not in supplied_scheme:
            raise ValueError("URL scheme must be http or https")
        else:
            # A bare domain with a port, such as example.com:8443.
            candidate = f"https://{raw}"
    else:
        candidate = f"https://{raw}"

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exception:
        raise ValueError("URL contains an invalid host or port") from exception
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("URL scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    if not parsed.hostname:
        raise ValueError("URL must include a valid hostname")

    hostname = parsed.hostname.casefold()
    try:
        ipaddress.ip_address(hostname)
        normalized_host = hostname
    except ValueError:
        if hostname.endswith("."):
            raise ValueError("URL hostname cannot end with a dot")
        try:
            normalized_host = hostname.encode("idna").decode("ascii")
        except UnicodeError as exception:
            raise ValueError("URL hostname is malformed") from exception
        labels = normalized_host.split(".")
        if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
            raise ValueError("URL must include a valid public hostname")

    netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _country_key(value: str) -> str:
    return " ".join(value.strip().casefold().split())


COUNTRY_ALIASES: dict[str, str] = {
    "uk": "GB",
    "u.k.": "GB",
    "great britain": "GB",
    "usa": "US",
    "u.s.a.": "US",
    "united states of america": "US",
    "uae": "AE",
    "south korea": "KR",
    "north korea": "KP",
    "russia": "RU",
    "vietnam": "VN",
    "ivory coast": "CI",
}
AMBIGUOUS_COUNTRY_NAMES = frozenset({"congo", "korea"})
COUNTRY_DISPLAY_OVERRIDES = {
    "BO": "Bolivia",
    "BN": "Brunei",
    "CD": "Democratic Republic of the Congo",
    "CG": "Republic of the Congo",
    "CI": "Côte d'Ivoire",
    "IR": "Iran",
    "KP": "North Korea",
    "KR": "South Korea",
    "LA": "Laos",
    "MD": "Moldova",
    "PS": "Palestine",
    "RU": "Russia",
    "SY": "Syria",
    "TZ": "Tanzania",
    "US": "United States",
    "VE": "Venezuela",
    "VN": "Vietnam",
}

_COUNTRIES_BY_INPUT: dict[str, str] = {}
_COUNTRY_NAMES_BY_CODE: dict[str, str] = {}
for _country in pycountry.countries:
    _code = _country.alpha_2
    _display = COUNTRY_DISPLAY_OVERRIDES.get(_code, _country.name)
    _COUNTRY_NAMES_BY_CODE[_code] = _display
    for _candidate in (
        _country.alpha_2,
        _country.alpha_3,
        _country.name,
        getattr(_country, "official_name", None),
        getattr(_country, "common_name", None),
        _display,
    ):
        if _candidate:
            _COUNTRIES_BY_INPUT[_country_key(_candidate)] = _code
for _alias, _alias_code in COUNTRY_ALIASES.items():
    _COUNTRIES_BY_INPUT[_country_key(_alias)] = _alias_code


def country_name_to_code(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    key = _country_key(raw)
    if key in AMBIGUOUS_COUNTRY_NAMES:
        raise ValueError(
            "Country name is ambiguous; enter a specific recognized country name"
        )
    code = _COUNTRIES_BY_INPUT.get(key)
    if code is None:
        raise ValueError(
            "Country is not recognized; enter a country name such as Bangladesh or an ISO code"
        )
    return code


def normalize_country_name(value: object) -> str | None:
    code = country_name_to_code(value)
    return _COUNTRY_NAMES_BY_CODE[code] if code is not None else None


def country_catalog_names() -> list[str]:
    return sorted(_COUNTRY_NAMES_BY_CODE.values())
