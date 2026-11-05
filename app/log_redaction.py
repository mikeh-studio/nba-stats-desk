"""Redact recognizable secrets from local diagnostic text."""

import re

SECRET_KEY_PATTERN = (
    r"password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|client[_-]?secret|credential"
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)\b({SECRET_KEY_PATTERN})(\s*[=:]\s*)[^\s,;]+"
)
SECRET_JSON_PATTERN = re.compile(
    rf"(?i)([\"'](?:{SECRET_KEY_PATTERN})[\"']\s*:\s*)[\"'][^\"']+[\"']"
)
SECRET_AUTH_PATTERN = re.compile(
    r"(?i)\b(authorization:\s*bearer\s+)[A-Za-z0-9._~+/=-]+"
)
SECRET_SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def redact_text(text: str, *, max_chars: int | None = None) -> str:
    redacted = SECRET_JSON_PATTERN.sub(
        lambda match: f'{match.group(1)}"<redacted>"', text
    )
    redacted = SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2<redacted>", redacted)
    redacted = SECRET_AUTH_PATTERN.sub(r"\1<redacted>", redacted)
    redacted = SECRET_SK_PATTERN.sub("sk-<redacted>", redacted)
    if max_chars is not None and len(redacted) > max_chars:
        return redacted[-max_chars:]
    return redacted
