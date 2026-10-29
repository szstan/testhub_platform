import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone

from apps.executions.models import ExecutionArtifact, ExecutionJob
from apps.quality_intelligence.models import QualityEvidence, QualityFailure, QualityFailureCluster, QualityRun


class Command(BaseCommand):
    help = 'Report bounded execution/QI scale and retention health; read-only.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', dest='as_json', help='Emit JSON output.')
        parser.add_argument('--project', type=int, default=None, help='Limit execution/project-backed counts to one project.')

    def handle(self, *args, **options):
        project_id = options.get('project')
        jobs = ExecutionJob.objects.all()
        if project_id:
            jobs = jobs.filter(project_id=project_id)
        artifact_scope = Q(job__in=jobs) | Q(attempt__job__in=jobs) | Q(step_result__attempt__job__in=jobs)
        artifacts = ExecutionArtifact.objects.filter(artifact_scope).distinct()
        summary = artifacts.aggregate(
            count=Count('id'),
            bytes=Sum('size_bytes'),
            oldest=Min('created_at'),
            latest=Max('created_at'),
        )
        now = timezone.now()
        quality_runs_visible = QualityRun.objects.all()
        unmapped_quality_runs = 0
        if project_id:
            mapped_run_filter = Q(project_type__in=['PROJECT', 'EXECUTION'], project_id=str(project_id))
            quality_runs = quality_runs_visible.filter(mapped_run_filter)
            unmapped_quality_runs = quality_runs_visible.exclude(mapped_run_filter).count()
        else:
            quality_runs = quality_runs_visible
        clusters_visible = QualityFailureCluster.objects.all()
        unmapped_quality_clusters = 0
        if project_id:
            mapped_cluster_filter = Q(mapped_project_id=project_id) | Q(project_type__in=['PROJECT', 'EXECUTION'], project_id=str(project_id))
            quality_clusters = clusters_visible.filter(mapped_cluster_filter)
            unmapped_quality_clusters = clusters_visible.exclude(mapped_cluster_filter).count()
        else:
            quality_clusters = clusters_visible
        result = {
            'project': project_id,
            'generated_at': now,
            'execution_jobs': jobs.count(),
            'execution_artifacts': int(summary['count'] or 0),
            'execution_artifact_bytes': int(summary['bytes'] or 0),
            'expired_execution_artifacts': artifacts.filter(retention_until__isnull=False, retention_until__lt=now).count(),
            'execution_oldest_created_at': summary['oldest'],
            'execution_latest_created_at': summary['latest'],
            'quality_runs': quality_runs.count(),
            'quality_evidence': QualityEvidence.objects.filter(run__in=quality_runs).count(),
            'quality_failures': QualityFailure.objects.filter(run__in=quality_runs).count(),
            'quality_failure_clusters': quality_clusters.count(),
            'unmapped_quality_runs': unmapped_quality_runs,
            'unmapped_quality_clusters': unmapped_quality_clusters,
            'thresholds': {
                'execution_jobs': getattr(settings, 'EXECUTION_SCALE_WARNING_JOBS', 100000),
                'execution_artifacts': getattr(settings, 'EXECUTION_SCALE_WARNING_ARTIFACTS', 100000),
                'execution_artifact_bytes': getattr(settings, 'EXECUTION_SCALE_WARNING_ARTIFACT_BYTES', 107374182400),
                'quality_runs': getattr(settings, 'QI_SCALE_WARNING_RUNS', 100000),
                'quality_evidence': getattr(settings, 'QI_SCALE_WARNING_EVIDENCE', 250000),
            },
        }
        result['warnings'] = [
            key for key, value in (
                ('execution_jobs', result['execution_jobs']),
                ('execution_artifacts', result['execution_artifacts']),
                ('execution_artifact_bytes', result['execution_artifact_bytes']),
                ('quality_runs', result['quality_runs']),
                ('quality_evidence', result['quality_evidence']),
            ) if value >= result['thresholds'][key]
        ]
        if options.get('as_json'):
            self.stdout.write(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
            return
        self.stdout.write(f"execution_jobs={result['execution_jobs']}")
        self.stdout.write(f"execution_artifacts={result['execution_artifacts']} bytes={result['execution_artifact_bytes']}")
        self.stdout.write(f"expired_execution_artifacts={result['expired_execution_artifacts']}")
        self.stdout.write(f"quality_runs={result['quality_runs']} quality_evidence={result['quality_evidence']} quality_failures={result['quality_failures']} quality_failure_clusters={result['quality_failure_clusters']}")
        self.stdout.write(f"unmapped_quality_runs={result['unmapped_quality_runs']} unmapped_quality_clusters={result['unmapped_quality_clusters']}")
        self.stdout.write(f"warnings={','.join(result['warnings']) or 'none'}")
        self.stdout.write('read-only report; use cleanup_execution_artifacts --apply only in an approved retention window.')
