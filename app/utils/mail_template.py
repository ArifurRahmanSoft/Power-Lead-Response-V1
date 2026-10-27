import html
from html.parser import HTMLParser
import re
import uuid

import bleach
from bleach.css_sanitizer import CSSSanitizer

from app.utils.lead_normalization import normalize_country_name


class UnsafeTemplateHtmlError(ValueError):
    pass


class InvalidTemplateSubjectError(ValueError):
    pass


class UnknownPlaceholderError(ValueError):
    def __init__(self, names: set[str]):
        super().__init__("Unknown template placeholders")
        self.names = names


PLACEHOLDER_FIELDS: dict[str, str] = {
    "first_name": "Lead first name",
    "last_name": "Lead last name",
    "email": "Lead email",
    "phone": "Lead phone",
    "company": "Company name",
    "designation": "Lead designation",
    "service_requested": "Requested service",
    "city": "Lead city",
    "state": "Lead state or region",
    "country": "Canonical country name",
    "industry": "Company industry",
    "company_website": "Company website",
    "company_size": "Company size",
}
PLACEHOLDER_PATTERN = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
_ANY_PLACEHOLDER_PATTERN = re.compile(r"{{(.*?)}}", re.DOTALL)
_DANGEROUS_HTML = re.compile(
    r"<\s*(?:script|style|iframe|object|embed|svg|math|form|input|button|meta|link)\b"
    r"|\bon[a-z]+\s*=|(?:javascript|data|file|vbscript)\s*:"
    r"|expression\s*\(|url\s*\(",
    re.IGNORECASE,
)
_ALLOWED_TAGS = frozenset(
    {"p", "br", "strong", "b", "em", "i", "u", "ul", "ol", "li", "a", "span", "div", "img"}
)
_CSS_SANITIZER = CSSSanitizer(allowed_css_properties=["color", "text-align"])


def _allowed_attribute(tag: str, name: str, value: str) -> bool:
    del value
    if tag == "a":
        return name in {"href", "title"}
    if tag == "img":
        return name in {"data-asset-id", "alt", "title"}
    if tag in {"p", "div", "span"}:
        return name == "style"
    return False


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "div", "li", "br"}:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "img":
            alt = next((value for name, value in attrs if name == "alt" and value), None)
            self.parts.append(alt or "[Image]")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


class _AssetReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.asset_ids: set[uuid.UUID] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        raw = next((value for name, value in attrs if name == "data-asset-id"), None)
        if not raw:
            raise UnsafeTemplateHtmlError("Uploaded images must include data-asset-id")
        try:
            self.asset_ids.add(uuid.UUID(raw))
        except ValueError as exception:
            raise UnsafeTemplateHtmlError("Image asset ID is invalid") from exception


def html_to_plain_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def extract_asset_ids(value: str) -> set[uuid.UUID]:
    parser = _AssetReferenceParser()
    parser.feed(value)
    parser.close()
    return parser.asset_ids


def sanitize_template_html(value: str, *, required: bool) -> tuple[str, str]:
    raw = value.strip()
    if _DANGEROUS_HTML.search(raw):
        raise UnsafeTemplateHtmlError("HTML contains unsafe markup, attributes, or URLs")
    cleaned = bleach.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes=_allowed_attribute,
        protocols={"http", "https", "mailto"},
        css_sanitizer=_CSS_SANITIZER,
        strip=True,
        strip_comments=True,
    ).strip()
    plain = html_to_plain_text(cleaned)
    assets = extract_asset_ids(cleaned)
    if required and not plain and not assets:
        raise UnsafeTemplateHtmlError("Template body must contain text or an uploaded image")
    if not required and cleaned and not plain and not assets:
        raise UnsafeTemplateHtmlError("Signature must contain text or an uploaded image")
    return cleaned, plain


def validate_subject(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise InvalidTemplateSubjectError("Subject cannot contain newlines")
    subject = " ".join(value.strip().split())
    if not subject:
        raise InvalidTemplateSubjectError("Subject is required")
    if len(subject) > 255:
        raise InvalidTemplateSubjectError("Subject must be at most 255 characters")
    return subject


def extract_placeholders(*values: str) -> set[str]:
    return {
        match.group(1)
        for value in values
        for match in PLACEHOLDER_PATTERN.finditer(value)
    }


def validate_placeholders(*values: str) -> set[str]:
    malformed: set[str] = set()
    for value in values:
        tokens = _ANY_PLACEHOLDER_PATTERN.findall(value)
        remainder = _ANY_PLACEHOLDER_PATTERN.sub("", value)
        if "{{" in remainder or "}}" in remainder:
            malformed.add("malformed")
        for token in tokens:
            cleaned = token.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cleaned):
                malformed.add(cleaned[:80] or "malformed")
    if malformed:
        raise UnknownPlaceholderError(malformed)
    names = extract_placeholders(*values)
    unknown = names - PLACEHOLDER_FIELDS.keys()
    if unknown:
        raise UnknownPlaceholderError(unknown)
    return names


def render_text_template(
    value: str,
    values: dict[str, object],
    *,
    escape_html: bool,
) -> tuple[str, set[str]]:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        supplied = values.get(name)
        if supplied is None or str(supplied).strip() == "":
            missing.add(name)
            return match.group(0)
        rendered = str(supplied)
        if "\r" in rendered or "\n" in rendered:
            if not escape_html:
                missing.add(name)
                return match.group(0)
        return html.escape(rendered, quote=True) if escape_html else rendered

    return PLACEHOLDER_PATTERN.sub(replace, value), missing


def lead_placeholder_values(lead: object) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in PLACEHOLDER_FIELDS:
        value = getattr(lead, field, None)
        if field == "country" and value:
            value = normalize_country_name(value)
        values[field] = value
    return values


def replace_asset_images_with_cids(value: str) -> str:
    pattern = re.compile(
        r"<img(?P<before>[^>]*?)\sdata-asset-id=(?P<quote>['\"])(?P<id>[0-9a-fA-F-]{36})(?P=quote)(?P<after>[^>]*)>",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        return (
            f'<img{match.group("before")} src="cid:template-asset-{match.group("id")}"'
            f'{match.group("after")}>'
        )

    return pattern.sub(replace, value)
