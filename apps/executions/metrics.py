"""Read-only, feature-gated quality projections for the execution control plane."""
from django.conf import settings
from django.db.models import Q

from apps.core.db_expressions import canonical_id_text


_QUALITY_COUNTS = (
    'flaky_profiles', 'quality_runs', 'quality_evidence', 'quality_failures',
    'quality_failure_clusters', 'unmapped_quality_runs', 'unmapped_quality_clusters',
)


def scoped_quality_metrics(user, projects, *, project_id=None):
    """Keep quality-data gates authoritative even through cross-module metrics."""
    enabled = bool(getattr(settings, 'QUALITY_INTELLIGENCE_ENABLED', False))
    result = dict.fromkeys(_QUALITY_COUNTS, 0)
    result['quality_intelligence_enabled'] = enabled
    if not enabled:
        return result
    if project_id is not None and not projects.exists():
        # Preserve the legacy empty response for inaccessible project filters;
        # do not populate its unmapped counters from the caller's other scopes.
        return result

    from apps.quality_intelligence.models import (
        QualityEvidence, QualityFailure, QualityFailureCluster, QualityRun,
        QualityTestProfile,
    )
    from apps.quality_intelligence.permissions import quality_project_filter

    scoped_ids = projects.order_by().annotate(
        quality_scope_id=canonical_id_text(using=projects.db),
    ).values('quality_scope_id')
    result['flaky_profiles'] = QualityTestProfile.objects.filter(
        project_type='PROJECT', project_id__in=scoped_ids,
        reliability_status='FLAKY',
    ).count()

    runs = quality_project_filter(QualityRun.objects.all(), user)
    if project_id is not None:
        mapped = Q(project_type='PROJECT', project_id=str(project_id))
        result['unmapped_quality_runs'] = runs.exclude(mapped).count()
        runs = runs.filter(mapped)
    result['quality_runs'] = runs.count()
    result['quality_evidence'] = QualityEvidence.objects.filter(run__in=runs).count()
    result['quality_failures'] = QualityFailure.objects.filter(run__in=runs).count()

    clusters = quality_project_filter(QualityFailureCluster.objects.all(), user)
    if project_id is not None:
        mapped = Q(mapped_project_id=project_id) | Q(
            project_type='PROJECT', project_id=str(project_id),
        )
        result['unmapped_quality_clusters'] = clusters.exclude(mapped).count()
        clusters = clusters.filter(mapped)
    result['quality_failure_clusters'] = clusters.count()
    return result
