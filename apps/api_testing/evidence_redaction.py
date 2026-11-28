"""In-memory value scrubbing shared by API cases, requests, and suites."""
import html
import json
import re
from urllib.parse import quote, quote_plus

from apps.quality_intelligence.services.redaction import REDACTION_MARKER


def _remember_text(value, literals):
    if not value:
        return
    literals.add(value)
    for encoded in (
        json.dumps(value, ensure_ascii=False)[1:-1],
        json.dumps(value, ensure_ascii=True)[1:-1],
        quote(value, safe=''),
        quote_plus(value, safe=''),
        html.escape(value, quote=True),
    ):
        if encoded:
            literals.add(encoded)


def remember_runtime_values(value, literals):
    if isinstance(value, dict):
        for item in value.values():
            remember_runtime_values(item, literals)
    elif isinstance(value, (list, tuple)):
        for item in value:
            remember_runtime_values(item, literals)
    elif value is not None and value != '':
        text = str(value)
        _remember_text(text, literals)
        if not isinstance(value, str):
            try:
                _remember_text(json.dumps(value, ensure_ascii=False), literals)
            except (TypeError, ValueError):
                pass


def _is_encoded_representation(value):
    return bool(
        re.search(r'%[0-9a-fA-F]{2}', value)
        or re.search(r'&(?:#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);', value)
        or re.search(r'\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|["\\/bfnrt])', value)
    )


def _looks_like_url(value):
    stripped = value.strip()
    if stripped.startswith(('http://', 'https://', '//', '/')) or '://' in stripped:
        return True
    if stripped.startswith(('{', '[')):
        return False
    return not any(char.isspace() for char in stripped) and '/' in stripped


def _is_url_key(value):
    if not isinstance(value, str):
        return False
    key = value.casefold().replace('-', '_')
    return key in {'url', 'uri', 'href', 'location', 'link', 'referer', 'referrer'} or key.endswith('_url')


def runtime_scrubber(literals):
    """Build a scrubber that runs before any evidence truncation or persistence."""
    alternatives = []
    for value in sorted(literals, key=lambda item: (-len(item), item)):
        # Percent-escape hex is case-insensitive; surrounding plaintext is not.
        escaped = re.sub(
            r'%[0-9a-fA-F]{2}',
            lambda match: f'(?i:{match.group(0)})',
            re.escape(value),
        )
        encoded = _is_encoded_representation(value)
        is_reserved_word = value.casefold() in {'true', 'false', 'null', 'none'}
        use_word_boundaries = (len(value) < 4 or value.isdecimal() or is_reserved_word) and not encoded
        if use_word_boundaries:
            if any(char.isalnum() for char in value):
                escaped = rf'(?<![\w]){escaped}(?![\w])'
            else:
                continue
        elif not any(char.isalnum() for char in value) and not encoded:
            continue
        alternatives.append(escaped)
    pattern = re.compile('|'.join([re.escape(REDACTION_MARKER)] + alternatives)) if literals else None
    raw_ampersand = re.compile(re.escape('&')) if '&' in literals else None

    def scrub(value, preserve_url=False):
        if isinstance(value, dict):
            return {
                scrub(key): scrub(item, preserve_url=_is_url_key(key))
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [scrub(item, preserve_url=preserve_url) for item in value]
        if isinstance(value, str):
            if value in literals:
                return REDACTION_MARKER
            result = pattern.sub(REDACTION_MARKER, value) if pattern else value
            if raw_ampersand and not preserve_url and not _looks_like_url(value):
                result = raw_ampersand.sub(REDACTION_MARKER, result)
            return result
        if pattern and value is not None and str(value) in literals:
            return REDACTION_MARKER
        return value

    return scrub
