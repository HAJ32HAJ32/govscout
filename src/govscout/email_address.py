from __future__ import annotations


FORBIDDEN_RECIPIENT_CHARACTERS = frozenset(",;<>\r\n\t ")


def normalise_single_recipient(value: str) -> str:
    """Return one canonical mailbox address or reject address lists/header syntax."""
    if not isinstance(value, str):
        raise ValueError("email must be a single recipient address")
    email = value.strip().lower()
    if (
        len(email) > 254
        or email.count("@") != 1
        or any(character in FORBIDDEN_RECIPIENT_CHARACTERS for character in email)
        or any(ord(character) < 33 or ord(character) == 127 for character in email)
    ):
        raise ValueError("email must be a single recipient address")
    local_part, domain = email.split("@", 1)
    if not local_part or not domain or local_part.startswith(".") or local_part.endswith("."):
        raise ValueError("email must be a single recipient address")
    return email
