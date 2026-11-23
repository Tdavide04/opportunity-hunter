"""Secret masking shared by notification channels and logs."""
import re

_SECRET_PATTERNS = [
    (re.compile(r"(api\.telegram\.org/bot)[^/\s]+"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)\S+"), r"\1***"),
    (re.compile(r"(?i)([?&](?:token|api_key|key|password)=)[^&\s]+"), r"\1***"),
    (re.compile(r"(://[^:/\s]+:)[^@\s]+(@)"), r"\1***\2"),
]


def mask_secrets(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    for secret in extra_secrets:
        if secret:
            text = text.replace(secret, "***")
    return text
