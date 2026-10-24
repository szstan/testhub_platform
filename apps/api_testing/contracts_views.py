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

    @action(detail=True, methods=['get'], url_path='generate-cases')
    def generate_cases(self, request, pk=None):
        return Response({'cases': generate_contract_cases(self.get_object())})
