"""Query parameter filters shared by quality-intelligence list endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import ValidationError

from apps.quality_intelligence.services.redaction import bound_text, redact_text


def _date_value(value: str, field: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is not None:
        return parsed
    date_value = parse_date(value)
    if date_value is not None:
        return datetime.combine(date_value, datetime.min.time())
    raise ValidationError({field: f"invalid ISO date/datetime: {value}"})


def apply_quality_filters(queryset: Any, params: Any, *, date_field: str = "created_at"):
    """Apply bounded, explicit filters; unknown parameters are ignored."""

    direct_fields = {field.name for field in queryset.model._meta.get_fields()}
    for field in (
        "source_type",
        "source_scope",
        "quality_status",
        "source_status",
        "project_type",
        "project_id",
        "environment_fingerprint",
        "taxonomy_code",
        "outcome",
        "reliability_status",
        "verdict",
        "metric_key",
    ):
        value = params.get(field)
        if value not in (None, "") and field in direct_fields:
            queryset = queryset.filter(**{field: value})
    run_id = params.get("run_id") or params.get("run")
    if run_id not in (None, "") and "run" in direct_fields:
        queryset = queryset.filter(run_id=run_id)
    profile_id = params.get("profile_id") or params.get("profile")
    if profile_id not in (None, "") and "profile" in direct_fields:
        queryset = queryset.filter(profile_id=profile_id)

        search = bound_text(redact_text(search, max_length=255), 255)
        if hasattr(queryset.model, "name"):
            queryset = queryset.filter(name__icontains=search)
        elif hasattr(queryset.model, "test_key"):
            queryset = queryset.filter(test_key__icontains=search)
    for param, lookup in (("created_after", "gte"), ("created_before", "lte")):
        value = params.get(param)
        if value not in (None, ""):
            if date_field not in direct_fields:
                continue
            queryset = queryset.filter(**{f"{date_field}__{lookup}": _date_value(value, param)})
    return queryset


__all__ = ["apply_quality_filters"]
