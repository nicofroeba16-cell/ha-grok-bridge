from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from typing import Any
from urllib.parse import urlparse

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


_CSRF_SECRET = secrets.token_bytes(32)


def is_loopback_host(value: str | None) -> bool:
    host = str(value or "").strip().lower().strip("[]")
    if host == "localhost":
        return True
    if "%" in host:
        host = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_loopback_request(*, client_host: str | None, request_url: str, origin: str | None = None) -> bool:
    if not is_loopback_host(client_host):
        return False
    request = urlparse(request_url)
    if request.scheme not in {"http", "https"} or not is_loopback_host(request.hostname):
        return False
    if origin is None:
        return True
    source = urlparse(origin)
    if source.scheme != request.scheme or not is_loopback_host(source.hostname):
        return False
    request_host = str(request.hostname or "").strip().lower().strip("[]")
    source_host = str(source.hostname or "").strip().lower().strip("[]")
    if request_host != source_host:
        return False
    source_port = source.port or (443 if source.scheme == "https" else 80)
    request_port = request.port or (443 if request.scheme == "https" else 80)
    return source_port == request_port


def issue_csrf(idempotency_key: str, *, now: int | None = None) -> str:
    stamp = int(time.time() if now is None else now)
    nonce = secrets.token_urlsafe(16)
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
    body = f"{stamp}.{nonce}.{key_hash}"
    signature = hmac.new(_CSRF_SECRET, body.encode(), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{body}.{encoded}"


def verify_csrf(token: str, idempotency_key: str, *, now: int | None = None, max_age: int = 600) -> bool:
    try:
        stamp_raw, nonce, key_hash, encoded = token.split(".", 3)
        stamp = int(stamp_raw)
    except (AttributeError, ValueError):
        return False
    current = int(time.time() if now is None else now)
    if stamp > current + 30 or current - stamp > max_age or not nonce:
        return False
    expected_key = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
    if not hmac.compare_digest(key_hash, expected_key):
        return False
    body = f"{stamp}.{nonce}.{key_hash}"
    signature = hmac.new(_CSRF_SECRET, body.encode(), hashlib.sha256).digest()
    expected = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return hmac.compare_digest(encoded, expected)
