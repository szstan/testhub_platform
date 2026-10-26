"""Durable Celery dispatcher contract for unified execution adapters."""

from __future__ import annotations

import re
from typing import Any

from celery import current_app
from django.conf import settings


_TASK_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _routes() -> dict[str, str]:
    configured = getattr(settings, "EXECUTION_DISPATCH_ROUTES", {})
    if isinstance(configured, dict):
        raw_routes = configured
    else:
        raw_routes = {}
        for item in str(configured or "").split(","):
            source, separator, task_name = item.partition("=")
            if separator and source.strip() and task_name.strip():
                raw_routes[source.strip()] = task_name.strip()

    routes: dict[str, str] = {}
    for source_type, value in raw_routes.items():
        task_name = value.get("task") if isinstance(value, dict) else value
        task_name = str(task_name or "").strip()
        if not task_name:
            continue
        if not _TASK_NAME_RE.fullmatch(task_name):
            raise ValueError(
                f"Invalid unified execution dispatcher task for {source_type!r}: {task_name!r}"
            )
        routes[str(source_type).strip()] = task_name
    return routes


class CeleryExecutionDispatcher:
    """Send a unified attempt to a configured, durable Celery task route.

    The destination task owns the source-specific execution. TestHub only
    sends a fenced job/attempt contract and records the broker dispatch id;
    it never claims that a source runner exists when the route is absent.
    """

    def route_for(self, source_type: str) -> str:
        return _routes().get(str(source_type or "").strip(), "")

    def supports(self, source_type: str) -> bool:
        return bool(self.route_for(source_type))

    def __call__(self, job: Any, attempt: Any, variant: Any = None) -> str:
        source_type = str(getattr(job, "source_type", "") or "").strip()
        task_name = self.route_for(source_type)
        if not task_name:
            raise RuntimeError(
                f"No unified execution dispatcher route configured for source type {source_type!r}"
            )
        if not getattr(job, "source_id", ""):
            raise ValueError(f"Unified execution source_id is required for source type {source_type!r}")

        variant_id = getattr(variant, "pk", None)
        task_id = f"execution-{job.pk}-{attempt.pk}"
        if variant_id is not None:
            task_id = f"{task_id}-variant-{variant_id}"
        async_result = current_app.send_task(
            task_name,
            args=[job.pk, attempt.pk],
            kwargs={
                "execution_job_id": job.pk,
                "attempt_id": attempt.pk,
                "source_type": source_type,
                "source_id": str(job.source_id),
                "variant_id": variant_id,
            },
            task_id=task_id,
        )
        return str(getattr(async_result, "id", "") or task_id)


__all__ = ["CeleryExecutionDispatcher"]
