"""Read-only source adapter registry for quality intelligence ingestion."""

from __future__ import annotations

from typing import Any

from .app import APPAdapter, AppAdapter, AppSourceAdapter
from .api import APIAdapter, ApiAdapter, ApiSourceAdapter
from .base import *  # noqa: F401,F403 - protocol is public package API
from .base import AdapterError
from .perf import PERFAdapter, PerfAdapter, PerfSourceAdapter, PerformanceSourceAdapter
from .ui import (
    UIAdapter,
    UISourceAdapter,
    UICaseAdapter,
    UiAdapter,
    UiCaseSourceAdapter,
    UiSourceAdapter,
)


ADAPTER_REGISTRY = {
    "API": ApiSourceAdapter,
    "UI": UiSourceAdapter,
    "APP": AppSourceAdapter,
    "PERF": PerfSourceAdapter,
}
SOURCE_ADAPTERS = ADAPTER_REGISTRY
ADAPTERS = ADAPTER_REGISTRY


def get_adapter(source_type: Any, **kwargs: Any) -> Any:
    """Return a configured adapter for a source type without touching the DB."""

    raw = getattr(source_type, "value", source_type)
    key = str(raw or "").strip().upper()
    try:
        adapter_class = ADAPTER_REGISTRY[key]
    except KeyError as exc:
        raise AdapterError(f"unsupported quality source type: {source_type!r}") from exc
    return adapter_class(**kwargs)


def get_source_adapter(source_type: Any, **kwargs: Any) -> Any:
    return get_adapter(source_type, **kwargs)


def adapter_for(source_type: Any, **kwargs: Any) -> Any:
    return get_adapter(source_type, **kwargs)


__all__ = [
    "ADAPTER_REGISTRY",
    "SOURCE_ADAPTERS",
    "ADAPTERS",
    "get_adapter",
    "get_source_adapter",
    "adapter_for",
    "ApiSourceAdapter",
    "APIAdapter",
    "ApiAdapter",
    "UiSourceAdapter",
    "UISourceAdapter",
    "UIAdapter",
    "UiAdapter",
    "UiCaseSourceAdapter",
    "UICaseAdapter",
    "AppSourceAdapter",
    "APPAdapter",
    "AppAdapter",
    "PerfSourceAdapter",
    "PerformanceSourceAdapter",
    "PERFAdapter",
    "PerfAdapter",
]
