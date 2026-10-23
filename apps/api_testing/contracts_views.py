import hashlib
import json

from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db import models

from apps.projects.models import Project

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

    @action(detail=True, methods=['get'], url_path='generate-cases')
    def generate_cases(self, request, pk=None):
        return Response({'cases': generate_contract_cases(self.get_object())})
