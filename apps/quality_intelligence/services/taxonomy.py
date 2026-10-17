"""Deterministic quality-failure taxonomy.

Classification is rule based and intentionally contains no provider or network
calls.  Rules are evaluated in the documented priority order so the same
failure evidence always yields the same code, classifier, and rule id.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from typing import Any


class FailureTaxonomy(str, Enum):
    SLA_BREACH = "SLA_BREACH"
    ASSERTION = "ASSERTION"
    HTTP_STATUS = "HTTP_STATUS"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    LOCATOR = "LOCATOR"
    DEVICE = "DEVICE"
    DRIVER = "DRIVER"
    ENVIRONMENT = "ENVIRONMENT"
    RESOURCE = "RESOURCE"
    HARNESS = "HARNESS"
    UNKNOWN = "UNKNOWN"


# A tuple, rather than iterating enum declaration order implicitly, makes the
# precedence part of the public contract and easy to inspect in tests.
TAXONOMY_PRIORITY = (
    FailureTaxonomy.SLA_BREACH.value,
    FailureTaxonomy.ASSERTION.value,
    FailureTaxonomy.HTTP_STATUS.value,
    FailureTaxonomy.TIMEOUT.value,
    FailureTaxonomy.NETWORK.value,
    FailureTaxonomy.LOCATOR.value,
    FailureTaxonomy.DEVICE.value,
    FailureTaxonomy.DRIVER.value,
    FailureTaxonomy.ENVIRONMENT.value,
    FailureTaxonomy.RESOURCE.value,
    FailureTaxonomy.HARNESS.value,
    FailureTaxonomy.UNKNOWN.value,
)

RULE_IDS = {
    code: f"QI-03A.TAXONOMY.{code}" for code in TAXONOMY_PRIORITY
}

# Coarse values are the choices currently persisted by QualityFailure.  The
# exact rule code remains available as ``taxonomy_code``/``code`` in service
# output; ``category`` and ``model_category`` are safe persistence adapters.
MODEL_CATEGORY = {
    "SLA_BREACH": "PERFORMANCE",
    "ASSERTION": "PRODUCT",
    "HTTP_STATUS": "PRODUCT",
    "TIMEOUT": "TIMEOUT",
    "NETWORK": "INFRASTRUCTURE",
    "LOCATOR": "TEST",
    "DEVICE": "ENVIRONMENT",
    "DRIVER": "ENVIRONMENT",
    "ENVIRONMENT": "ENVIRONMENT",
    "RESOURCE": "INFRASTRUCTURE",
    "HARNESS": "TEST",
    "UNKNOWN": "UNKNOWN",
}

_RETRYABLE = {
    "SLA_BREACH": False,
    "ASSERTION": False,
    "HTTP_STATUS": False,
    "TIMEOUT": True,
    "NETWORK": True,
    "LOCATOR": False,
    "DEVICE": True,
    "DRIVER": True,
    "ENVIRONMENT": False,
    "RESOURCE": True,
    "HARNESS": False,
    "UNKNOWN": False,
}

# Rules use complete words where possible to avoid classifying a normal test
# name containing e.g. ``driver`` as an error from an unrelated field.
_PATTERNS = {
    "SLA_BREACH": re.compile(
        r"(?:sla\s*[_ -]?\s*breach|service\s+level\s+(?:agreement|breach)|"
        r"latency\s+budget|budget\s+exceed(?:ed|s)?|contract\s+breach)",
        re.IGNORECASE,
    ),
    "ASSERTION": re.compile(
        r"(?:assert(?:ion)?(?:error|\s+(?:failed|failure))|expected\s*[:=].*"
        r"actual\s*[:=]|actual\s*[:=].*expected\s*[:=])",
        re.IGNORECASE | re.DOTALL,
    ),
    "HTTP_STATUS": re.compile(
        r"(?:http(?:\s*(?:4\d{2}|5\d{2})\b|\s*error\b|\s+status(?:\s+code)?)|"
        r"status\s*code|response\s+status)",
        re.IGNORECASE,
    ),
    "TIMEOUT": re.compile(
        r"(?:timeout|timed\s+out|deadline\s+exceed(?:ed|s)?|read\s+timeout|"
        r"connect\s+timeout|etimedout)",
        re.IGNORECASE,
    ),
    "NETWORK": re.compile(
        r"(?:network|connection|connect(?:ion)?\s+(?:refused|reset|failed|"
        r"aborted|closed)|dns|name\s+resolution|socket|unreachable|"
        r"broken\s+pipe|ssl\s+error|econn(?:reset|refused)|enotfound)",
        re.IGNORECASE,
    ),
    "LOCATOR": re.compile(
        r"(?:locator|selector|xpath|css\s+selector|no\s+such\s+element|"
        r"nosuchelement|stale\s+element|staleelement|element\s+(?:not\s+found|"
        r"not\s+visible|not\s+interactable|intercepted)|elementnotfound|"
        r"cannot\s+find\s+element)",
        re.IGNORECASE,
    ),
    "DEVICE": re.compile(
        r"(?:\bdevice\b|adb|emulator|simulator|handset|mobile\s+device|"
        r"device\s+(?:offline|unavailable|not\s+found))",
        re.IGNORECASE,
    ),
    "DRIVER": re.compile(
        r"(?:webdriver|web\s*driver|chromedriver|geckodriver|msedgedriver|"
        r"browser\s+driver|nosuchdriver|sessionnotcreated|driver(?:exception|"
        r"\s+(?:error|failed|crash)))",
        re.IGNORECASE,
    ),
    "ENVIRONMENT": re.compile(
        r"(?:environment|configuration|config\s+(?:error|missing|invalid)|"
        r"dependency|version\s+mismatch|platform\s+(?:error|unsupported)|"
        r"executable\s+not\s+found|browser\s+not\s+installed|"
        r"permission\s+denied)",
        re.IGNORECASE,
    ),
    "RESOURCE": re.compile(
        r"(?:out\s+of\s+(?:memory|space|resources)|memory\s+error|memoryerror|"
        r"disk\s+full|no\s+space\s+left|quota|too\s+many\s+open\s+files|"
        r"resource\s+(?:exhausted|limit))",
        re.IGNORECASE,
    ),
    "HARNESS": re.compile(
        r"(?:harness|test\s+runner|runner\s+error|fixture|teardown|"
        r"setup\s+(?:error|failed)|pytest|unittest|orchestrat(?:or|ion))",
        re.IGNORECASE,
    ),
}

_FLAG_KEYS = frozenset(
    {
        "sla_breach",
        "sla_breached",
        "service_level_breach",
        "deadline_breached",
        "budget_exceeded",
        "contract_breach",
        "sla_violation",
        "sla_status",
    }
)
_STATUS_KEYS = frozenset(
    {
        "status",
        "status_code",
        "statuscode",
        "http_status",
        "http_status_code",
        "response_status",
        "response_status_code",
    }
)
_EXPLICIT_CODE_KEYS = frozenset(
    {
        "code",
        "failure_code",
        "taxonomy",
        "taxonomy_code",
        "category",
        "failure_category",
        "source_error_code",
        "error_type",
        "exception_type",
    }
)


class FailureClassification(dict):
    """Mapping result with attribute access for adapter ergonomics."""

    def __init__(
        self,
        code: str,
        *,
        rule_id: str,
        confidence: float,
        retryable: bool,
        model_category: str,
        subtype: str = "",
    ) -> None:
        super().__init__(
            taxonomy_code=code,
            code=code,
            category=model_category,
            model_category=model_category,
            initial_taxonomy_code=code,
            classifier="RULE",
            rule_id=rule_id,
            confidence=confidence,
            retryable=retryable,
            subtype=subtype,
        )

    @property
    def code(self) -> str:
        return self["code"]

    @property
    def taxonomy_code(self) -> str:
        return self["taxonomy_code"]

    @property
    def category(self) -> str:
        return self["category"]

    @property
    def classifier(self) -> str:
        return self["classifier"]

    @property
    def rule_id(self) -> str:
        return self["rule_id"]

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-friendly copy."""

        return dict(self)

    def to_model_fields(self) -> dict[str, Any]:
        """Return fields accepted by the current QualityFailure model."""

        fields = self.as_dict()
        fields["taxonomy_code"] = self["model_category"]
        fields["initial_taxonomy_code"] = self["model_category"]
        return fields


# Common alternate names used by adapters.
FailureCode = FailureTaxonomy
Classification = FailureClassification
TaxonomyResult = FailureClassification


def _normalize_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _walk_evidence(value: Any, *, key: str = "") -> tuple[list[str], list[tuple[str, Any]], set[str]]:
    """Collect text, key/value pairs, and explicit boolean flags privately."""

    texts: list[str] = []
    pairs: list[tuple[str, Any]] = []
    flags: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            normalized = _normalize_key(raw_key)
            pairs.append((normalized, child))
            if normalized in _FLAG_KEYS and (
                child is True
                or (isinstance(child, (int, float)) and not isinstance(child, bool) and child == 1)
                or (isinstance(child, str) and child.strip().lower() in {"1", "true", "yes", "on", "breached", "violation"})
            ):
                flags.add(normalized)
            if key in {"sla", "sla_status", "service_level"} and normalized in {"breach", "breached", "violation"} and (
                child is True
                or (isinstance(child, (int, float)) and not isinstance(child, bool) and child == 1)
                or (isinstance(child, str) and child.strip().lower() in {"1", "true", "yes", "on", "breached", "violation"})
            ):
                flags.add("sla_breach")
            child_texts, child_pairs, child_flags = _walk_evidence(
                child,
                key=normalized,
            )
            texts.extend(child_texts)
            pairs.extend(child_pairs)
            flags.update(child_flags)
        return texts, pairs, flags
    if isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            child_texts, child_pairs, child_flags = _walk_evidence(child, key=key)
            texts.extend(child_texts)
            pairs.extend(child_pairs)
            flags.update(child_flags)
        return texts, pairs, flags
    if isinstance(value, (str, int, float, bool)):
        texts.append(str(value))
    elif value is not None:
        texts.append(type(value).__name__)
        texts.append(str(value))
    return texts, pairs, flags


def _error_evidence(error: Any) -> tuple[dict[str, Any], str]:
    if error is None:
        return {}, ""
    if isinstance(error, Mapping):
        values = dict(error)
        text = " ".join(
            [type(error).__name__]
            + [str(value) for value in values.values() if value is not None]
        )
        return values, text
    if isinstance(error, BaseException):
        return {}, f"{type(error).__name__} {error}"
    return {}, str(error)


def _sla_numeric_breach(pairs: list[tuple[str, Any]]) -> bool:
    """Detect an explicit duration-over-budget signal without heuristics."""

    durations: list[float] = []
    limits: list[float] = []
    duration_keys = {"duration_ms", "elapsed_ms", "latency_ms", "response_time_ms"}
    limit_keys = {"sla_ms", "sla_limit_ms", "latency_budget_ms", "max_duration_ms"}
    for key, value in pairs:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if key in duration_keys:
            durations.append(number)
        elif key in limit_keys:
            limits.append(number)
    return bool(durations and limits and max(durations) > min(limits))


def _status_values(pairs: list[tuple[str, Any]], explicit: Any) -> list[int]:
    values: list[int] = []
    if explicit is not None:
        candidates = [explicit]
    else:
        candidates = [value for key, value in pairs if key in _STATUS_KEYS]
    for candidate in candidates:
        try:
            status = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            values.append(status)
    return values


def _explicit_code(pairs: list[tuple[str, Any]]) -> str | None:
    for key, value in pairs:
        if key not in _EXPLICIT_CODE_KEYS:
            continue
        normalized = _normalize_key(value).upper()
        if normalized in TAXONOMY_PRIORITY and normalized != "UNKNOWN":
            return normalized
    return None


def _subtype(code: str, *, statuses: list[int], text: str) -> str:
    if code == "HTTP_STATUS" and statuses:
        return f"status_{statuses[0]}"
    if code == "ASSERTION" and "assertionerror" in text.lower():
        return "assertion_error"
    return code.lower()


def classify_failure(
    error: Any = None,
    context: Mapping[str, Any] | None = None,
    *,
    message: str | None = None,
    status_code: int | str | None = None,
    error_code: str | None = None,
    phase: str | None = None,
    **metadata: Any,
) -> FailureClassification:
    """Classify failure evidence using deterministic priority rules.

    ``error`` may be an exception, text, or mapping.  ``context`` and keyword
    metadata are inspected for status/flag evidence but never copied into the
    result, so a secret-bearing error message cannot leak through taxonomy.
    """

    values, error_text = _error_evidence(error)
    evidence: dict[str, Any] = {}
    evidence.update(values)
    if context:
        evidence["context"] = context
    evidence.update(metadata)
    if message is not None:
        evidence["message"] = message
    if error_code is not None:
        evidence["error_code"] = error_code
    if phase is not None:
        evidence["phase"] = phase

    texts, pairs, flags = _walk_evidence(evidence)
    if error_text:
        texts.append(error_text)
    joined_text = " ".join(texts)
    statuses = _status_values(pairs, status_code)
    if _sla_numeric_breach(pairs):
        flags.add("sla_breach")
    if any(status >= 400 for status in statuses):
        joined_text += " http status code"
    if flags:
        joined_text += " sla breach"

    matched: str | None = None
    for code in TAXONOMY_PRIORITY[:-1]:
        if code == "SLA_BREACH" and flags:
            matched = code
            break
        if code == "HTTP_STATUS" and any(status >= 400 for status in statuses):
            matched = code
            break
        if _PATTERNS[code].search(joined_text):
            matched = code
            break

    # Explicit codes are useful when the source runner has already normalized
    # evidence, but natural evidence still wins according to priority above.
    if matched is None:
        matched = _explicit_code(pairs) or "UNKNOWN"

    return FailureClassification(
        matched,
        rule_id=RULE_IDS[matched],
        confidence=1.0 if matched != "UNKNOWN" else 0.0,
        retryable=_RETRYABLE[matched],
        model_category=MODEL_CATEGORY[matched],
        subtype=_subtype(matched, statuses=statuses, text=joined_text),
    )


def classify_error(*args: Any, **kwargs: Any) -> FailureClassification:
    """Compatibility alias for :func:`classify_failure`."""

    return classify_failure(*args, **kwargs)


def classify(*args: Any, **kwargs: Any) -> FailureClassification:
    """Short alias for :func:`classify_failure`."""

    return classify_failure(*args, **kwargs)


def determine_taxonomy(*args: Any, **kwargs: Any) -> str:
    """Return only the exact taxonomy code for simple adapters."""

    return classify_failure(*args, **kwargs).taxonomy_code


__all__ = [
    "FailureClassification",
    "FailureCode",
    "FailureTaxonomy",
    "Classification",
    "MODEL_CATEGORY",
    "RULE_IDS",
    "TAXONOMY_PRIORITY",
    "TaxonomyResult",
    "classify",
    "classify_error",
    "classify_failure",
    "determine_taxonomy",
]
