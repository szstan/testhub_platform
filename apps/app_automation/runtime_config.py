"""Validation for non-secret APP scene runtime settings."""

from urllib.parse import urlsplit


def validate_base_url(value):
    """Validate syntax only; outbound destination checks occur for every request."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('base_url must be an HTTP(S) URL of at most 2048 characters')
    if not value:
        return value
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('base_url contains invalid whitespace or control characters')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or
                (port is not None and not 1 <= port <= 65535)):
            raise ValueError
    except ValueError:
        raise ValueError('base_url must be an HTTP(S) URL without credentials, query or fragment') from None
    return value
