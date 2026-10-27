import re


E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")


def normalize_e164_phone(value: object) -> str:
    if value is None:
        raise ValueError("Lead phone is required for SMS sending")
    phone = str(value).strip()
    if not phone:
        raise ValueError("Lead phone is required for SMS sending")
    if not E164_PATTERN.fullmatch(phone):
        raise ValueError(
            "Lead phone must use international E.164 format, for example +14155552671"
        )
    return phone
