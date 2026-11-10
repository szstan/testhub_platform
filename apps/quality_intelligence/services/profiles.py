"""Batch reliability projections with bounded raw history windows."""

from collections import defaultdict

from django.db import transaction
from django.db.models import Case, F, When, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from apps.quality_intelligence.models import QualityRunItem, QualityTestProfile
from apps.quality_intelligence.services.flakiness import (
    DEFAULT_WINDOW_SIZE, MIN_ELIGIBLE_ATTEMPTS, calculate_flakiness,
)
from apps.quality_intelligence.services.identity import effective_environment
from apps.quality_intelligence.services.redaction import redact_payload


@transaction.atomic
def refresh_profiles(run, items):
    """Refresh each distinct identity once, retaining latest raw rows before SKIP exclusion."""
    identities = {
        (item.test_key, effective_environment(item.environment_fingerprint, run.environment_fingerprint)): item.name
        for item in items
    }
    if not identities:
        return {}
    identity_filter = dict(source_type=run.source_type, project_type=run.project_type, project_id=run.project_id)
    keys = {key for key, _env in identities}
    environments = {env for _key, env in identities}
    query = QualityTestProfile.objects.filter(
        **identity_filter, test_key__in=keys, environment_fingerprint__in=environments,
    )
    profiles = {(p.test_key, p.environment_fingerprint): p for p in query.select_for_update()}
    missing = [
        QualityTestProfile(
            **identity_filter, test_key=key, environment_fingerprint=env, name=name,
            window_size=DEFAULT_WINDOW_SIZE, min_attempts=MIN_ELIGIBLE_ATTEMPTS,
        )
        for (key, env), name in identities.items() if (key, env) not in profiles
    ]
    if missing:
        QualityTestProfile.objects.bulk_create(missing, ignore_conflicts=True, batch_size=200)
        profiles = {(p.test_key, p.environment_fingerprint): p for p in query.select_for_update()}
    profiles = {identity: profiles[identity] for identity in identities}
    max_window = max(profile.window_size for profile in profiles.values())
    # DB rank first; calculate_flakiness then applies the unchanged eligibility algorithm.
    history = QualityRunItem.objects.filter(
        run__source_type=run.source_type,
        run__project_type=run.project_type,
        run__project_id=run.project_id,
        test_key__in=keys,
    ).annotate(
        profile_environment=Case(
            When(environment_fingerprint__in=['', 'unknown'], then=F('run__environment_fingerprint')),
            default=F('environment_fingerprint'),
        ),
    ).filter(profile_environment__in=environments).annotate(
        recent_rank=Window(
            expression=RowNumber(),
            partition_by=[F('test_key'), F('profile_environment')],
            order_by=[F('created_at').desc(), F('id').desc()],
        ),
    ).filter(recent_rank__lte=max_window).order_by('created_at', 'id').values(
        'id', 'test_key', 'name', 'outcome', 'profile_environment', 'created_at',
        source_type=F('run__source_type'), project_type=F('run__project_type'),
        project_id=F('run__project_id'), observed_at=F('run__finished_at'),
    )
    grouped = defaultdict(list)
    for row in history:
        row['environment_fingerprint'] = row.pop('profile_environment')
        grouped[(row['test_key'], row['environment_fingerprint'])].append(row)
    now = timezone.now()
    for identity, profile in profiles.items():
        candidates = grouped[identity][-profile.window_size:]
        analysis = calculate_flakiness(
            candidates, window_size=profile.window_size, min_attempts=profile.min_attempts,
            test_key=profile.test_key, environment_fingerprint=profile.environment_fingerprint,
            **identity_filter,
        )
        timestamps = [row['observed_at'] or row['created_at'] for row in candidates]
        if profile.first_seen_at:
            timestamps.append(profile.first_seen_at)
        if timestamps:
            profile.first_seen_at = min(timestamps)
            profile.last_seen_at = max([*timestamps, *([profile.last_seen_at] if profile.last_seen_at else [])])
        profile.name = next((row['name'] for row in reversed(candidates) if row['name']), profile.name)
        for field in ('eligible_attempts', 'pass_count', 'fail_count', 'transition_count', 'flaky_score', 'reliability_status'):
            setattr(profile, field, analysis[field])
        profile.metadata = redact_payload({
            'reason': analysis['reason'], 'window_count': analysis['window_count'],
            'source': 'unified_execution' if run.project_type == 'PROJECT' else 'source_execution',
        })
        profile.updated_at = now
    QualityTestProfile.objects.bulk_update(list(profiles.values()), [
        'name', 'eligible_attempts', 'pass_count', 'fail_count', 'transition_count',
        'flaky_score', 'reliability_status', 'first_seen_at', 'last_seen_at', 'metadata', 'updated_at',
    ], batch_size=200)
    return profiles
