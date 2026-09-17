from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:token|secret|password|passwd|authorization|cookie|private[_-]?key|api[_-]?key|"
    r"credential|client[_-]?secret|access[_-]?key|refresh[_-]?token)",
    re.IGNORECASE,
)
_VALUE_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{12,}\b"), _REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), _REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), _REDACTED),
    (re.compile(r"(?i)\b(token|secret|password|passwd|api[_-]?key)\s*[:=]\s*[^\s,;]+"), r"\1=[REDACTED]"),
    (re.compile(r"(https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE), r"\1[REDACTED]@"),
)


def redact_text(value: str) -> str:
    result = value
    for pattern, replacement in _VALUE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def sanitize(value: Any, key: str | None = None) -> Any:
    """Return a JSON-safe view with obvious secret-bearing keys and values redacted."""
    if key and _SENSITIVE_KEY.search(key):
        return _REDACTED
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
