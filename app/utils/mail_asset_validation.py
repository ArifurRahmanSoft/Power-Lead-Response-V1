from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
import zipfile

from PIL import Image, UnidentifiedImageError


class InvalidMailAssetError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedMailAsset:
    filename: str
    content_type: str
    data: bytes
    sha256: str


_BLOCKED_SUFFIXES = frozenset(
    {".exe", ".dll", ".com", ".scr", ".msi", ".bat", ".cmd", ".ps1", ".js", ".jar", ".html", ".htm", ".svg"}
)
_HTML_PREFIX = re.compile(rb"\s*(?:<!doctype\s+html|<html\b|<script\b|<svg\b|<\?xml)", re.IGNORECASE)
Image.MAX_IMAGE_PIXELS = 25_000_000


def _safe_filename(value: str | None) -> str:
    name = Path(value or "upload").name.strip()
    if not name or len(name) > 255 or any(ord(character) < 32 for character in name):
        raise InvalidMailAssetError("Filename is invalid")
    if Path(name).suffix.lower() in _BLOCKED_SUFFIXES:
        raise InvalidMailAssetError("This file type is not allowed")
    return name


def _detect_zip_type(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names:
                return None
            lowered = {name.casefold() for name in names}
            if any(
                "vbaproject.bin" in name
                or name.startswith("word/embeddings/")
                or name.startswith("xl/embeddings/")
                for name in lowered
            ):
                return None
            if any(name.startswith("word/") for name in names):
                return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if any(name.startswith("xl/") for name in names):
                return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    except (zipfile.BadZipFile, OSError):
        return None
    return None


def _detect_content_type(data: bytes) -> str | None:
    image_headers = (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith((b"GIF87a", b"GIF89a"))
        or (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )
    if image_headers:
        try:
            with Image.open(BytesIO(data)) as image:
                detected_format = str(image.format or "").upper()
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            return None
        return {
            "PNG": "image/png",
            "JPEG": "image/jpeg",
            "GIF": "image/gif",
            "WEBP": "image/webp",
        }.get(detected_format)
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return _detect_zip_type(data)
    if data.startswith((b"MZ", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")):
        return None
    if b"\x00" in data[:8192] or _HTML_PREFIX.match(data[:1024]):
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "text/plain"


def validate_mail_asset(
    filename: str | None,
    data: bytes,
    *,
    kind: str,
) -> ValidatedMailAsset:
    safe_name = _safe_filename(filename)
    if not data:
        raise InvalidMailAssetError("Uploaded file is empty")
    detected = _detect_content_type(data)
    image_types = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    attachment_types = image_types | {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/plain",
    }
    allowed = image_types if kind in {"inline_body", "signature_image"} else attachment_types
    if detected not in allowed:
        raise InvalidMailAssetError("File content type is not allowed")
    if detected == "text/plain" and Path(safe_name).suffix.lower() not in {".txt", ".csv"}:
        raise InvalidMailAssetError("Text attachments must use .txt or .csv")
    return ValidatedMailAsset(
        filename=safe_name,
        content_type=detected,
        data=data,
        sha256=sha256(data).hexdigest(),
    )
