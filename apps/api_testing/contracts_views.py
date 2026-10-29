import hashlib
import json

from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db import models

from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.executions.models import ExecutionJob
from apps.executions.result_events import ResultEventError, ResultEventIngestor

from .contract_runner import execute_contract_cases
from .contracts import coverage_for_spec, diff_contracts, generate_contract_cases
from .models import OpenApiContractSpec


class OpenApiContractSpecSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    document_hash = serializers.CharField(read_only=True)

    class Meta:
        model = OpenApiContractSpec
        fields = '__all__'
        read_only_fields = ('id', 'created_by', 'document_hash', 'created_at', 'updated_at')

    def validate_project(self, value):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated or not Project.objects.filter(pk=value.pk).filter(
            models.Q(owner=user) | models.Q(members=user)
        ).exists():
            raise serializers.ValidationError('项目不存在或无权限')
        return value

    def validate_document(self, value):
        if not isinstance(value, dict) or not isinstance(value.get('paths'), dict):
            raise serializers.ValidationError('document 必须包含 paths 对象')
        return value


class OpenApiContractSpecViewSet(viewsets.ModelViewSet):
    serializer_class = OpenApiContractSpecSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        projects = Project.objects.all() if user.is_staff or user.is_superuser else Project.objects.filter(
            models.Q(owner=user) | models.Q(members=user)
        ).distinct()
        return OpenApiContractSpec.objects.filter(project__in=projects).select_related('project', 'created_by')

    def perform_create(self, serializer):
        document = serializer.validated_data['document']
        instance = serializer.save(
            created_by=self.request.user,
            document_hash=hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        )
        return instance

    def perform_update(self, serializer):
        document = serializer.validated_data.get('document', serializer.instance.document)
        serializer.save(
            project=serializer.instance.project,
            document_hash=hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        )

    @action(detail=True, methods=['post'])
    def diff(self, request, pk=None):
        current = self.get_object()
        baseline = self.get_queryset().filter(pk=request.data.get('baseline_id'), project=current.project).first()
        if baseline is None:
            return Response({'detail': 'baseline_id 不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
        return Response(diff_contracts(current, baseline))

    @action(detail=True, methods=['get'])
    def coverage(self, request, pk=None):
        return Response(coverage_for_spec(self.get_object()))

    @action(detail=True, methods=['post'], url_path='materialize-cases')
    def materialize_cases(self, request, pk=None):
        spec = self.get_object()
        limit = min(max(int(request.data.get('limit', 500)), 1), 500)
        selected_kinds = request.data.get('kinds')
        if selected_kinds and not isinstance(selected_kinds, list):
            return Response({'detail': 'kinds 必须是数组'}, status=status.HTTP_400_BAD_REQUEST)
        cases = generate_contract_cases(spec)
        if selected_kinds:
            cases = [case for case in cases if case.get('kind') in selected_kinds]
        cases = cases[:limit]
        created = []
        updated = []
        for case in cases:
            external_ref = case['external_ref']
            values = {
                'title': f"{case['method']} {case['path']} [{case['kind']}]"[:500],
                'description': json.dumps({
                    'operation_id': case.get('operation_id'),
                    'summary': case.get('summary'),
                    'strategy': case.get('strategy', 'schema-driven'),
                    'source_spec': spec.pk,
                    'spec_version': spec.version,
                    'contract_key': case.get('contract_key'),
                    'case_kind': case.get('case_kind', case.get('kind')),
                }, ensure_ascii=False),
                'preconditions': 'OpenAPI contract generated case',
                'steps': f"{case['method']} {case['path']}",
                'expected_result': '响应符合 OpenAPI contract 且满足测试场景',
                'priority': 'high' if case['kind'] == 'security' else 'medium',
                'test_type': 'security' if case['kind'] == 'security' else 'api',
                'status': 'draft',
                'author': request.user,
            }
            testcase, was_created = TestCase.objects.get_or_create(
                project=spec.project,
                external_ref=external_ref,
                defaults=values,
            )
            if not was_created:
                for key, value in values.items():
                    if key not in {'author'}:
                        setattr(testcase, key, value)
                testcase.save(update_fields=[key for key in values if key != 'author'] + ['updated_at'])
                updated.append({'id': testcase.id, 'external_ref': external_ref})
            else:
                created.append({'id': testcase.id, 'external_ref': external_ref})
        return Response({
            'spec_id': spec.pk,
            'created': created,
            'updated': updated,
            'total': len(created) + len(updated),
            'next_step': '使用这些 TestCase.external_ref 组织测试套件并通过 CI/ExecutionJob 执行',
        })

    @action(detail=True, methods=['post'], url_path='execute')
    def execute_contract(self, request, pk=None):
        spec = self.get_object()
        job = ExecutionJob.objects.filter(pk=request.data.get('job_id'), project=spec.project).first()
        if job is None:
            return Response({'detail': 'job_id 不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
        if job.is_terminal or job.final_attempt_id:
            return Response({'detail': 'job 已有终态结果，请创建新的 job 记录 contract run'}, status=status.HTTP_409_CONFLICT)
        try:
            results = execute_contract_cases(
                spec,
                base_url=str(request.data.get('base_url') or ''),
                headers=request.data.get('headers') or {},
                kinds=request.data.get('kinds') if isinstance(request.data.get('kinds'), list) else None,
                limit=request.data.get('limit', 100),
                timeout=request.data.get('timeout', 15),
            )
            next_attempt_number = (job.attempts.order_by('-attempt_number').values_list('attempt_number', flat=True).first() or 0) + 1
            ResultEventIngestor.ingest(job, 'run_started', {'attempt': next_attempt_number}, idempotency_key=f'contract-runner:{spec.pk}:{job.pk}')
            for row in results:
                ResultEventIngestor.ingest(
                    job,
                    'step_finished',
                    {
                        'attempt': next_attempt_number,
                        'case_key': row['contract_key'],
                        'step_key': f"contract:{row['case_kind']}",
                        'step_idempotency_key': f"contract-runner-step:{spec.pk}:{job.pk}:{row['contract_key']}",
                        'status': row['status'],
                        'result': row.get('result') or {},
                        'error_message': row.get('error_message', ''),
                        'duration_ms': row.get('duration_ms'),
                    },
                    idempotency_key=f"contract-runner-event:{spec.pk}:{job.pk}:{row['contract_key']}",
                )
            final_status = 'failed' if any(row['status'] in {'failed', 'blocked'} for row in results) else 'passed'
            ResultEventIngestor.ingest(
                job,
                'run_finished',
                {
                    'attempt': next_attempt_number,
                    'status': final_status,
                    'result': {
                        'contract_spec_id': spec.pk,
                        'contract_version': spec.version,
                        'total': len(results),
                        'failed': sum(row['status'] in {'failed', 'blocked'} for row in results),
                        'unmatched': 0,
                    },
                },
                idempotency_key=f'contract-runner-finish:{spec.pk}:{job.pk}',
            )
        except (TypeError, ValueError, ResultEventError) as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'spec_id': spec.pk, 'job_id': job.pk, 'status': final_status, 'results': results, 'coverage': coverage_for_spec(spec)}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='record-results')
    def record_results(self, request, pk=None):
        spec = self.get_object()
        job_id = request.data.get('job_id')
        rows = request.data.get('results')
        if not job_id or not isinstance(rows, list) or not rows:
            return Response(
                {'detail': 'job_id 和非空 results 数组均为必填项'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        job = ExecutionJob.objects.filter(pk=job_id, project=spec.project).first()
        if job is None:
            return Response({'detail': 'job_id 不存在或无权限'}, status=status.HTTP_404_NOT_FOUND)
        if job.is_terminal or job.final_attempt_id:
            return Response({'detail': 'job 已有终态结果，请创建新的 job 记录 contract run'}, status=status.HTTP_409_CONFLICT)
        next_attempt_number = (job.attempts.order_by('-attempt_number').values_list('attempt_number', flat=True).first() or 0) + 1
        expected = generate_contract_cases(spec)
        expected_by_key = {}
        for case in expected:
            for key in (case['contract_key'], case['external_ref']):
                expected_by_key[key] = case
        normalized = []
        for row in rows[:500]:
            if not isinstance(row, dict):
                return Response({'detail': 'results 中每一项必须是对象'}, status=status.HTTP_400_BAD_REQUEST)
            supplied_key = str(row.get('contract_key') or row.get('case_key') or '').strip()
            operation_key = str(row.get('operation_key') or '').strip()
            kind = str(row.get('case_kind') or row.get('kind') or '').strip().lower()
            case = expected_by_key.get(supplied_key)
            if case is None and operation_key and kind:
                case = next(
                    (item for item in expected if item['operation_key'] == operation_key and item['case_kind'] == kind),
                    None,
                )
            if case is None:
                return Response(
                    {'detail': f'contract case 不存在: {supplied_key or operation_key}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            row_status = str(row.get('status') or '').strip().lower()
            if row_status in {'pass', 'success'}:
                row_status = 'passed'
            if row_status not in {'passed', 'succeeded', 'completed', 'failed', 'blocked', 'skipped'}:
                return Response({'detail': f'不支持的 contract result status: {row_status}'}, status=status.HTTP_400_BAD_REQUEST)
            normalized.append((case, row_status, row))

        try:
            start = ResultEventIngestor.ingest(
                job,
                'run_started',
                {'attempt': next_attempt_number},
                idempotency_key=f'contract-run:{spec.pk}:{job.pk}',
            )
            for case, row_status, row in normalized:
                ResultEventIngestor.ingest(
                    job,
                    'step_finished',
                    {
                        'attempt': next_attempt_number,
                        'case_key': case['contract_key'],
                        'step_key': f"contract:{case['case_kind']}",
                        'step_idempotency_key': f"contract-step:{spec.pk}:{job.pk}:{case['contract_key']}",
                        'status': row_status,
                        'result': row.get('result') or {},
                        'error_message': str(row.get('error_message') or '')[:4000],
                        'duration_ms': row.get('duration_ms'),
                    },
                    idempotency_key=f"contract-step-event:{spec.pk}:{job.pk}:{case['contract_key']}",
                )
            final_status = 'failed' if any(row_status in {'failed', 'blocked'} for _, row_status, _ in normalized) else 'passed'
            ResultEventIngestor.ingest(
                job,
                'run_finished',
                {
                    'attempt': next_attempt_number,
                    'status': final_status,
                    'result': {
                        'contract_spec_id': spec.pk,
                        'contract_version': spec.version,
                        'total': len(normalized),
                        'failed': sum(row_status in {'failed', 'blocked'} for _, row_status, _ in normalized),
                        'unmatched': 0,
                    },
                },
                idempotency_key=f'contract-finish:{spec.pk}:{job.pk}',
            )
        except ResultEventError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'spec_id': spec.pk,
            'job_id': job.pk,
            'created_results': len(normalized),
            'status': final_status,
            'coverage': coverage_for_spec(spec),
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'], url_path='generate-cases')
    def generate_cases(self, request, pk=None):
        return Response({'cases': generate_contract_cases(self.get_object())})
