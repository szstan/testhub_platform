"""Django system checks for performance-test safety settings."""

from math import isfinite
from numbers import Real

from django.conf import settings
from django.core.checks import Error, Tags, register


_POSITIVE_SETTINGS = (
    'PERF_MAX_CONCURRENCY',
    'PERF_MAX_TARGET_RPS',
    'PERF_MAX_DURATION',
    'PERF_MAX_CONCURRENT_EXECUTIONS',
    'PERF_RETENTION_DAYS',
    'PERF_ARTIFACT_RETENTION_DAYS',
)


def _is_positive_number(value):
    return (
        isinstance(value, Real)
        and not isinstance(value, bool)
        and isfinite(value)
        and value > 0
    )


@register(Tags.compatibility)
def check_perf_settings(app_configs=None, **kwargs):
    """Catch unsafe performance limits before the service accepts traffic."""
    errors = []
    values = {}

    for name in _POSITIVE_SETTINGS:
        value = getattr(settings, name, None)
        if not _is_positive_number(value):
            errors.append(
                Error(
                    f'{name} must be a finite number greater than 0.',
                    hint=f'Set {name} to a positive value in the environment or settings.',
                    id='perf_testing.E001',
                )
            )
        else:
            values[name] = value

    cpu_warn = getattr(settings, 'PERF_LOAD_GEN_CPU_WARN', None)
    if not _is_positive_number(cpu_warn) or cpu_warn > 100:
        errors.append(
            Error(
                'PERF_LOAD_GEN_CPU_WARN must be a finite percentage between 0 and 100.',
                hint='Set PERF_LOAD_GEN_CPU_WARN to a value such as 85.',
                id='perf_testing.E002',
            )
        )

    retention_days = values.get('PERF_RETENTION_DAYS')
    artifact_days = values.get('PERF_ARTIFACT_RETENTION_DAYS')
    if retention_days is not None and artifact_days is not None and artifact_days > retention_days:
        errors.append(
            Error(
                'PERF_ARTIFACT_RETENTION_DAYS cannot exceed PERF_RETENTION_DAYS.',
                hint='Keep artifact retention no longer than execution-record retention.',
                id='perf_testing.E003',
            )
        )

    return errors
