"""Access control helpers for quality-intelligence projections."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models
from rest_framework.exceptions import NotFound
from rest_framework.permissions import BasePermission


_SOURCE_PROJECT_MODELS: dict[str, tuple[str, str]] = {
    "API": ("apps.api_testing.models", "ApiProject"),
    "UI": ("apps.ui_automation.models", "UiProject"),
    "APP": ("apps.app_automation.models", "AppProject"),
    "PERF": ("apps.perf_testing.models", "PerfProject"),
}


def _source_model(source_type: Any):
    source_key = str(getattr(source_type, "value", source_type) or "").upper()
    module_name, model_name = _SOURCE_PROJECT_MODELS.get(source_key, ("", ""))
    if not module_name:
        return None
    module = __import__(module_name, fromlist=[model_name])
    return getattr(module, model_name)


class QualityIntelligenceEnabledPermission(BasePermission):
    """Hide derived quality APIs until the rollout flag is explicitly enabled."""

    def has_permission(self, request, view):
        if not getattr(settings, "QUALITY_INTELLIGENCE_ENABLED", False):
            raise NotFound()
        return True


def is_privileged(user: Any) -> bool:
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def accessible_project_ids(user: Any, source_type: Any) -> set[str] | None:
    """Return project IDs visible to a user, or ``None`` for staff users."""

    if is_privileged(user):
        return None
    if not getattr(user, "is_authenticated", False):
        return set()
    model = _source_model(source_type)
    if model is None:
        return set()
    try:
        queryset = model.objects.filter(
            models.Q(owner=user) | models.Q(members=user)
        ).distinct()
        return {str(value) for value in queryset.values_list("pk", flat=True)}
    except Exception:
        return set()


def quality_project_filter(queryset: Any, user: Any, *, source_field: str = "source_type"):
    """Filter a quality queryset by its source type/project snapshot."""

    if is_privileged(user):
        return queryset
    visible: list[models.Q] = []
    for source_type in _SOURCE_PROJECT_MODELS:
        project_ids = accessible_project_ids(user, source_type)
        if project_ids:
            visible.append(
                models.Q(**{source_field: source_type}, project_id__in=project_ids)
            )
    if not visible:
        return queryset.none()
    condition = visible.pop()
    for item in visible:
        condition |= item
    return queryset.filter(condition).exclude(project_id="")


def source_project_id(source_type: Any, execution: Any) -> str:
    """Resolve a source execution's project without writing to the source app."""

    source_key = str(getattr(source_type, "value", source_type) or "").upper()
    if source_key == "API":
        suite = getattr(execution, "test_suite", None)
        project_id = getattr(suite, "project_id", None) if suite is not None else None
        return str(project_id) if project_id not in (None, "") else ""
    if source_key in {"PERF", "UI"}:
        value = getattr(execution, "project_id", None)
        return str(value) if value not in (None, "") else ""
    test_case = getattr(execution, "test_case", None)
    project_id = getattr(test_case, "project_id", None) if test_case is not None else None
    if project_id in (None, ""):
        suite = getattr(execution, "test_suite", None)
        project_id = getattr(suite, "project_id", None) if suite is not None else None
    return str(project_id) if project_id not in (None, "") else ""


def source_execution_queryset(source_type: Any, execution_id: Any):
    """Return one source execution using a read-only queryset, if present."""

    source_key = str(getattr(source_type, "value", source_type) or "").upper()
    if source_key == "API":
        from apps.api_testing.models import TestExecution

        return TestExecution.objects.select_related("test_suite", "test_suite__project").filter(pk=execution_id).first()
    if source_key == "UI":
        from apps.ui_automation.models import TestExecution

        return TestExecution.objects.select_related("project").filter(pk=execution_id).first()
    if source_key == "APP":
        from apps.app_automation.models import AppTestExecution

        return AppTestExecution.objects.select_related(
            "test_case__project", "test_suite__project"
        ).filter(pk=execution_id).first()
    if source_key == "PERF":
        from apps.perf_testing.models import PerfExecution

        return PerfExecution.objects.select_related("project").filter(pk=execution_id).first()
    return None


def can_access_source_execution(user: Any, source_type: Any, execution: Any) -> bool:
    if execution is None:
        return False
    project_id = source_project_id(source_type, execution)
    if not project_id:
        return False
    visible = accessible_project_ids(user, source_type)
    return visible is None or project_id in visible


__all__ = [
    "QualityIntelligenceEnabledPermission",
    "accessible_project_ids",
    "can_access_source_execution",
    "is_privileged",
    "quality_project_filter",
    "source_execution_queryset",
    "source_project_id",
]
