"""Small, deterministic redaction helpers for quality evidence.

The quality intelligence pipeline receives request/response payloads and error
text from several runners.  This module deliberately has no Django or runner
dependencies so it can be used at ingestion boundaries and in unit tests.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTION_VERSION = "1.0"
REDACTION_MARKER = "[REDACTED]"
TRUNCATION_MARKER = "[TRUNCATED]"
DEFAULT_MAX_TEXT_LENGTH = 4096
DEFAULT_MAX_PAYLOAD_ITEMS = 1000
DEFAULT_MAX_PAYLOAD_DEPTH = 12


# These names are normalized before matching, so ``apiKey``, ``api-key`` and
# ``API_KEY`` are treated alike.  Do not include a bare ``key`` or ``id``:
# those are common, safe pieces of test metadata.
_SENSITIVE_TERMS = frozenset(
    {
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "hmac",
        "jwt",
        "password",
        "passwd",
        "passphrase",
        "private_key",
        "secret",
        "session",
        "signature",
        "token",
        "webhook",
    }
)
_SENSITIVE_COMPOUND_TERMS = frozenset(
    {
        "access_key",
        "access_token",
        "client_key",
        "client_secret",
        "consumer_key",
        "encryption_key",
        "id_token",
        "refresh_token",
        "secret_key",
        "session_id",
        "set_cookie",
        "signing_key",
        "x_api_key",
        "x_auth_token",
        "x_token",
        "webhook_url",
    }
)

_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_-])"
    r"(?:authorization|proxy-authorization|cookie|set-cookie|token|"
    r"access[-_]?token|refresh[-_]?token|id[-_]?token|csrf[-_]?token|"
    r"session(?:[-_]?id)?|password|passwd|passphrase|secret|client[-_]?secret|"
    r"api[-_]?key|apikey|private[-_]?key|credential(?:s)?|webhook|"
    r"bearer)"
    r"(?![A-Za-z0-9_-])\s*[:=]\s*)"
    r"(?P<value>(?:(?:Bearer|Basic|Digest)\s+)?"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+))",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(
    r"(?P<prefix>\bBearer\s+)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_TOKEN_SPACE_RE = re.compile(
    r"(?P<prefix>\b(?:token|password|passwd|passphrase|secret)\s+)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_URL_RE = re.compile(
    r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+)",
    re.IGNORECASE,
)


def _normalize_key(key: Any) -> str:
    """Return a comparison form without changing the caller's key."""

    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def is_sensitive_key(key: Any) -> bool:
    """Whether a mapping key conventionally contains a secret value."""

    normalized = _normalize_key(key)
    if not normalized:
        return False
    if normalized in _SENSITIVE_TERMS or normalized in _SENSITIVE_COMPOUND_TERMS:
        return True

    parts = set(normalized.split("_"))
    if parts.intersection(
        {"authorization", "cookie", "credential", "credentials", "hmac", "signature", "webhook"}
    ):
        return True
    if "token" in parts or "secret" in parts or "password" in parts:
        return True
    if "api" in parts and "key" in parts:
        return True
    if "private" in parts and "key" in parts:
        return True
    if "signing" in parts and "key" in parts:
        return True
    if "secret" in parts and "key" in parts:
        return True
    if "session" in parts and "id" in parts:
        return True
    return False


def bound_text(
    value: Any,
    max_length: int = DEFAULT_MAX_TEXT_LENGTH,
    *,
    suffix: str = TRUNCATION_MARKER,
) -> str:
    """Coerce text to a bounded string without raising on unusual values."""

    text = "" if value is None else str(value)
    if max_length is None or max_length < 0 or len(text) <= max_length:
        return text
    if max_length == 0:
        return ""
    if not suffix:
        return text[:max_length]
    if len(suffix) >= max_length:
        return suffix[:max_length]
    return text[: max_length - len(suffix)] + suffix


def bounded_text(value: Any, max_length: int = DEFAULT_MAX_TEXT_LENGTH, **kwargs: Any) -> str:
    """Explicitly named alias for :func:`bound_text`."""

    return bound_text(value, max_length=max_length, **kwargs)


def truncate_text(value: Any, max_length: int = DEFAULT_MAX_TEXT_LENGTH, **kwargs: Any) -> str:
    """Explicitly named alias for :func:`bound_text`."""

    return bound_text(value, max_length=max_length, **kwargs)
def _redacted_value(value: str) -> str:
    """Keep quote style and auth scheme while replacing a value in text."""

    stripped = value.strip()
    for scheme in ("Bearer", "Basic", "Digest"):
        prefix = scheme + " "
        if stripped.lower().startswith(prefix.lower()):
            return prefix + REDACTION_MARKER
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[0] + REDACTION_MARKER + value[-1]
    return REDACTION_MARKER


def redact_url(
    value: Any,
    *,
    max_length: int = DEFAULT_MAX_TEXT_LENGTH,
) -> str:
    """Redact credentials and sensitive query parameters in a URL.

    Query ordering and repeated parameters are retained.  URL parsing can
    reject malformed ports or brackets; in that case the generic text rules
    still provide a conservative redaction.
    """

    text = "" if value is None else str(value)
    try:
        parsed = urlsplit(text)
        netloc = parsed.netloc
        if "@" in netloc:
            userinfo, host = netloc.rsplit("@", 1)
            if ":" in userinfo:
                username, _ = userinfo.split(":", 1)
                netloc = f"{username}:{REDACTION_MARKER}@{host}"
            else:
                netloc = f"{REDACTION_MARKER}@{host}"

        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        if query_items:
            query_items = [
                (key, REDACTION_MARKER if is_sensitive_key(key) else val)
                for key, val in query_items
            ]
            query = urlencode(query_items, doseq=True)
        else:
            query = parsed.query

        fragment = redact_text(parsed.fragment, max_length=max_length)
        redacted = urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))
    except (TypeError, ValueError):
        redacted = redact_text(text, max_length=max_length)
    return bound_text(redacted, max_length)


def redact_text(
    value: Any,
    *,
    max_length: int = DEFAULT_MAX_TEXT_LENGTH,
) -> str:
    """Redact common secret assignments, bearer values, and embedded URLs."""

    text = "" if value is None else str(value)

    def replace_url(match: re.Match[str]) -> str:
        return redact_url(match.group("url"), max_length=max_length)

    # URL handling first means query parameters use the same encoding rules as
    # standalone URLs.  Assignment rules then cover headers and JSON snippets.
    text = _URL_RE.sub(replace_url, text)
    text = _ASSIGNMENT_RE.sub(
        lambda match: match.group("prefix") + _redacted_value(match.group("value")),
        text,
    )
    text = _BEARER_RE.sub(
        lambda match: match.group("prefix") + REDACTION_MARKER,
        text,
    )
    text = _TOKEN_SPACE_RE.sub(
        lambda match: match.group("prefix") + REDACTION_MARKER,
        text,
    )
    return bound_text(text, max_length)


def redact_headers(
    headers: Any,
    *,
    max_text_length: int = DEFAULT_MAX_TEXT_LENGTH,
    max_items: int = DEFAULT_MAX_PAYLOAD_ITEMS,
) -> Any:
    """Redact a case-insensitive header mapping while preserving safe fields."""

    if isinstance(headers, Mapping):
        redacted: dict[Any, Any] = {}
        items = list(headers.items())
        for key, value in items[:max_items]:
            redacted[key] = (
                REDACTION_MARKER
                if is_sensitive_key(key)
                else redact_text(value, max_length=max_text_length)
                if isinstance(value, str)
                else redact_payload(
                    value,
                    max_text_length=max_text_length,
                    max_items=max_items,
                )
            )
        if len(items) > max_items:
            redacted["__truncated__"] = True
        return redacted
    if isinstance(headers, (list, tuple)):
        values = [
            redact_headers(item, max_text_length=max_text_length, max_items=max_items)
            if isinstance(item, Mapping)
            else redact_text(item, max_length=max_text_length)
            for item in headers[:max_items]
        ]
        if len(headers) > max_items:
            values.append({"__truncated__": True})
        return type(headers)(values)
    return redact_payload(
        headers,
        max_text_length=max_text_length,
        max_items=max_items,
    )


def redact_payload(
    value: Any,
    *,
    max_text_length: int = DEFAULT_MAX_TEXT_LENGTH,
    max_items: int = DEFAULT_MAX_PAYLOAD_ITEMS,
    max_depth: int = DEFAULT_MAX_PAYLOAD_DEPTH,
) -> Any:
    """Recursively redact dictionaries/lists and bound their size.

    The function returns a new container and does not mutate the input.  At a
    depth limit a truncation marker is used rather than returning unvisited
    data, so the bound never becomes a path around redaction.
    """

    def visit(item: Any, depth: int) -> Any:
        if isinstance(item, str):
            return redact_text(item, max_length=max_text_length)
        if isinstance(item, bytes):
            text = item.decode("utf-8", errors="replace")
            return redact_text(text, max_length=max_text_length).encode("utf-8")
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if depth >= max_depth:
            return TRUNCATION_MARKER
        if isinstance(item, Mapping):
            entries = list(item.items())
            output: dict[Any, Any] = {}
            for key, child in entries[:max_items]:
                output[key] = (
                    REDACTION_MARKER
                    if is_sensitive_key(key)
                    else visit(child, depth + 1)
                )
            if len(entries) > max_items:
                output["__truncated__"] = True
            return output
        if isinstance(item, list):
            output = [visit(child, depth + 1) for child in item[:max_items]]
            if len(item) > max_items:
                output.append(TRUNCATION_MARKER)
            return output
        if isinstance(item, tuple):
            output = [visit(child, depth + 1) for child in item[:max_items]]
            if len(item) > max_items:
                output.append(TRUNCATION_MARKER)
            return tuple(output)
        if isinstance(item, set):
            # Sets are uncommon in JSON payloads.  Sorting their bounded,
            # redacted text representation makes the result deterministic.
            output = [visit(child, depth + 1) for child in item]
            output.sort(key=repr)
            return output[:max_items]
        return redact_text(item, max_length=max_text_length)

    return visit(value, 0)


def redact_mapping(value: Any, **kwargs: Any) -> Any:
    """Compatibility alias for callers that explicitly handle mappings."""

    return redact_payload(value, **kwargs)


def bound_payload(value: Any, **kwargs: Any) -> Any:
    """Bound and redact an evidence payload in one pure operation."""

    return redact_payload(value, **kwargs)


def bounded_payload(value: Any, **kwargs: Any) -> Any:
    """Explicitly named alias for :func:`bound_payload`."""

    return bound_payload(value, **kwargs)


redact = redact_payload
sanitize = redact_payload


__all__ = [
    "DEFAULT_MAX_PAYLOAD_DEPTH",
    "DEFAULT_MAX_PAYLOAD_ITEMS",
    "DEFAULT_MAX_TEXT_LENGTH",
    "REDACTION_MARKER",
    "REDACTION_VERSION",
    "TRUNCATION_MARKER",
    "bound_payload",
    "bound_text",
    "bounded_payload",
    "bounded_text",
    "is_sensitive_key",
    "redact",
    "redact_headers",
    "redact_mapping",
    "redact_payload",
    "redact_text",
    "redact_url",
    "sanitize",
    "truncate_text",
]
