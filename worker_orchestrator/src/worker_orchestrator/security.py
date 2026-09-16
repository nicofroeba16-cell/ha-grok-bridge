from __future__ import annotations

import os
import re
from typing import Any


_SECRET_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[opusr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key)\b\s*[:=]\s*([^\s,;]+)"),
]


def known_secret_values() -> tuple[str, ...]:
    names = (
        "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GITLAB_TOKEN", "GITHUB_WEBHOOK_SECRET", "WORKER_TOKEN",
    )
    return tuple(v for name in names if (v := os.environ.get(name)) and len(v) >= 8)


def redact_text(value: str) -> str:
    text = value
    for secret in known_secret_values():
        text = text.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)\\b"):
            text = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(v) for v in value]
    return value
