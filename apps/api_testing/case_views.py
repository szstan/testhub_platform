"""HTTP API for reusable API test cases and mixed-suite membership."""

from __future__ import annotations

import hmac

from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.quality_intelligence.services.redaction import redact_payload

from .case_serializers import ApiCaseExecutionSerializer, ApiTestCaseSerializer, TestSuiteCaseSerializer
from .case_execution_snapshot import api_case_execution_fingerprint, api_case_execution_payload_hash
from .models import (
    ApiCaseExecution,
    ApiCollection,
    ApiProject,
    ApiRequest,
    ApiTestCase,
    ApiTestCaseStep,
    Environment,
    TestSuite,
    TestSuiteCase,
    TestSuiteRequest,
)
from .utils import can_access_api_project, can_use_environment


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'The requested change conflicts with current execution state.'
    default_code = 'conflict'


class CasePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def request_has_case_step_references(request_id):
    """Return whether deleting or moving an API request would break a case step."""
    return ApiTestCaseStep.objects.filter(request_id=getattr(request_id, 'pk', request_id)).exists()


def request_case_step_project_conflict(request_id, project_id):
    return ApiTestCaseStep.objects.filter(request_id=request_id).exclude(
        test_case__project_id=project_id,
    ).exists()


def accessible_api_projects(user):
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return ApiProject.objects.all()
    return ApiProject.objects.filter(Q(owner=user) | Q(members=user)).distinct()


class ApiTestCaseViewSet(viewsets.ModelViewSet):
    serializer_class = ApiTestCaseSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = CasePagination

    def get_queryset(self):
        queryset = ApiTestCase.objects.filter(project__in=accessible_api_projects(self.request.user))
        project_id = self.request.query_params.get('project')
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        for field in ('status', 'priority'):
            value = self.request.query_params.get(field)
            if value:
                queryset = queryset.filter(**{field: value})
        search = self.request.query_params.get('search') or self.request.query_params.get('q')
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(description__icontains=search)
                | Q(preconditions__icontains=search) | Q(expected_result__icontains=search)
            )
        return queryset.select_related('project', 'environment', 'linked_testcase', 'created_by').prefetch_related(
            'steps__request__collection',
        )

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        initial_instance = self.get_object()
        with transaction.atomic():
            instance = ApiTestCase.objects.select_for_update().select_related('project').filter(
                pk=initial_instance.pk,
            ).first()
            if instance is None or not can_access_api_project(instance.project, self.request.user):
                raise Http404
            if instance.project_id != initial_instance.project_id:
                raise Conflict('用例所属项目刚刚变更，请刷新后重试')
            target_project = serializer.validated_data.get('project', instance.project)
            if not can_access_api_project(target_project, self.request.user):
                raise PermissionDenied('无权使用目标项目')
            if instance.executions.filter(status__in=['PENDING', 'RUNNING']).exists():
                raise Conflict('用例有正在排队或执行的任务，暂不能编辑')
            if instance.suite_memberships.filter(test_suite__executions__status='RUNNING').exists():
                raise Conflict('用例正在某个测试套件中运行，暂不能编辑')
            serializer.instance = instance
            serializer.save()

    def perform_destroy(self, instance):
        with transaction.atomic():
            locked = ApiTestCase.objects.select_for_update().select_related('project').filter(
                pk=instance.pk,
            ).first()
            if locked is None or not can_access_api_project(locked.project, self.request.user):
                raise Http404
            if locked.suite_memberships.exists():
                raise Conflict('用例已加入测试套件，移除套件关联后才能删除')
            if locked.executions.filter(status__in=['PENDING', 'RUNNING']).exists():
                raise Conflict('用例有正在排队或执行的任务，暂不能删除')
            locked.delete()

    @action(detail=True, methods=['post'], url_path='duplicate')
    def duplicate(self, request, pk=None):
        source = self.get_object()
        with transaction.atomic():
            source = ApiTestCase.objects.select_for_update().select_related('project').prefetch_related(
                'steps',
            ).filter(pk=source.pk).first()
            if source is None or not can_access_api_project(source.project, request.user):
                raise Http404
            duplicate = ApiTestCase.objects.create(
                project=source.project,
                name=f'{source.name} (copy)',
                description=source.description,
                preconditions=source.preconditions,
                expected_result=source.expected_result,
                priority=source.priority,
                status='draft',
                tags=list(source.tags or []),
                variables=dict(source.variables or {}),
                environment=source.environment,
                stop_on_failure=source.stop_on_failure,
                linked_testcase=source.linked_testcase,
                created_by=request.user,
            )
            ApiTestCaseStep.objects.bulk_create([
                ApiTestCaseStep(
                    test_case=duplicate,
                    name=step.name,
                    order=step.order,
                    enabled=step.enabled,
                    request=step.request,
                    request_overrides=step.request_overrides,
                    assertions=step.assertions,
                    extractors=step.extractors,
                )
                for step in source.steps.all()
            ])
        return Response(self.get_serializer(duplicate).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='execute')
    def execute(self, request, pk=None):
        test_case = self.get_object()
        variables = request.data.get('variables', {})
        if not isinstance(variables, dict):
            raise ValidationError({'variables': '必须是JSON对象'})
        environment_id = request.data.get('environment_id')
        idempotency_key = request.data.get('idempotency_key', '') or ''
        expected_fingerprint = request.data.get('execution_fingerprint')
        if not isinstance(idempotency_key, str) or len(idempotency_key) > 128:
            raise ValidationError({'idempotency_key': '长度不能超过128个字符'})
        if expected_fingerprint is not None and (
            not isinstance(expected_fingerprint, str)
            or len(expected_fingerprint) != 64
            or any(character not in '0123456789abcdef' for character in expected_fingerprint)
        ):
            raise ValidationError({'execution_fingerprint': '执行配置指纹无效'})

        with transaction.atomic():
            ApiProject.objects.select_for_update().get(pk=test_case.project_id)
            locked_case = ApiTestCase.objects.select_for_update().select_related(
                'project',
            ).filter(pk=test_case.pk).first()
            if locked_case is None:
                raise Http404
            if locked_case.project_id != test_case.project_id:
                raise Conflict('用例所属项目刚刚变更，请刷新后重试')
            if not can_access_api_project(locked_case.project, request.user):
                raise Http404

            selected_environment_id = environment_id
            if selected_environment_id is None:
                selected_environment_id = locked_case.environment_id
            if selected_environment_id is None:
                environment = None
            else:
                try:
                    environment = Environment.objects.select_for_update().filter(
                        pk=selected_environment_id,
                    ).first()
                except (TypeError, ValueError, OverflowError):
                    environment = None
                if environment is None:
                    if environment_id is not None:
                        raise ValidationError({'environment_id': '执行环境不存在'})
                    selected_environment_id = None
            if environment and not can_use_environment(environment, request.user, project=locked_case.project):
                raise PermissionDenied('无权使用该执行环境')

            steps = list(ApiTestCaseStep.objects.select_for_update().filter(
                test_case=locked_case,
            ).order_by('order', 'pk'))
            request_ids = sorted({step.request_id for step in steps})
            request_locations = dict(ApiRequest.objects.filter(
                pk__in=request_ids,
            ).values_list('pk', 'collection_id'))
            if len(request_locations) != len(request_ids):
                raise Conflict('步骤请求刚刚变更，请刷新后重试')
            collection_ids = sorted({
                collection_id for collection_id in request_locations.values() if collection_id is not None
            })
            collections = {
                collection.pk: collection
                for collection in ApiCollection.objects.select_for_update().filter(
                    pk__in=collection_ids,
                ).order_by('pk')
            }
            if len(collections) != len(collection_ids):
                raise Conflict('步骤请求集合刚刚变更，请刷新后重试')
            requests = {
                api_request.pk: api_request
                for api_request in ApiRequest.objects.select_for_update().filter(
                    pk__in=request_ids,
                ).order_by('pk')
            }
            if len(requests) != len(request_ids):
                raise Conflict('步骤请求刚刚变更，请刷新后重试')
            for step in steps:
                api_request = requests[step.request_id]
                if api_request.collection_id != request_locations[api_request.pk]:
                    raise Conflict('步骤请求刚刚变更，请刷新后重试')
                api_request.collection = collections.get(api_request.collection_id)
                step.request = api_request

            snapshot_fingerprint = api_case_execution_fingerprint(
                locked_case, environment=environment, steps=steps,
            )
            if expected_fingerprint is not None and not hmac.compare_digest(
                expected_fingerprint, snapshot_fingerprint,
            ):
                raise Conflict('用例执行配置已变化，请刷新后重试')

            payload_hash = api_case_execution_payload_hash(
                snapshot_fingerprint,
                environment_id=selected_environment_id,
                test_case_id=locked_case.pk,
                variables=variables,
            )
            existing = None
            if idempotency_key:
                existing = ApiCaseExecution.objects.filter(
                    project=locked_case.project,
                    executed_by=request.user,
                    idempotency_key=idempotency_key,
                ).first()
                if existing:
                    if existing.idempotency_payload_hash != payload_hash or existing.test_case_id != locked_case.pk:
                        raise Conflict('幂等键已用于不同的用例或执行参数')
                    return Response(ApiCaseExecutionSerializer(existing).data)

            execution = ApiCaseExecution.objects.create(
                project=locked_case.project,
                test_case=locked_case,
                test_case_name=locked_case.name,
                environment=environment,
                executed_by=request.user,
                status='PENDING',
                idempotency_key=idempotency_key or None,
                idempotency_payload_hash=payload_hash,
            )

        from .case_tasks import execute_api_case_execution

        try:
            execute_api_case_execution.delay(execution.pk, variables, require_snapshot=True)
        except Exception as exc:  # broker failure is stored as a terminal execution
            execution.status = 'FAILED'
            execution.end_time = timezone.now()
            execution.error_message = redact_payload(str(exc))[:4000]
            execution.save(update_fields=['status', 'end_time', 'duration', 'error_message'])
            return Response(
                ApiCaseExecutionSerializer(execution).data,
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(ApiCaseExecutionSerializer(execution).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['get'], url_path='executions')
    def executions(self, request, pk=None):
        test_case = self.get_object()
        queryset = ApiCaseExecution.objects.filter(
            project_id=test_case.project_id,
            test_case_id=test_case.pk,
        ).select_related('test_case', 'environment', 'executed_by')
        from .case_tasks import reconcile_stale_api_case_executions
        reconcile_stale_api_case_executions(queryset=queryset)
        page = self.paginate_queryset(queryset)
        serializer = ApiCaseExecutionSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)


class ApiCaseExecutionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ApiCaseExecutionSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = CasePagination

    def get_queryset(self):
        queryset = ApiCaseExecution.objects.filter(
            project__in=accessible_api_projects(self.request.user),
        ).select_related('test_case', 'environment', 'executed_by', 'project')
        from .case_tasks import reconcile_stale_api_case_executions
        reconcile_stale_api_case_executions(queryset=queryset)
        return queryset


class TestSuiteCaseViewSet(viewsets.ModelViewSet):
    serializer_class = TestSuiteCaseSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = CasePagination
    http_method_names = ['get', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        return TestSuiteCase.objects.filter(
            test_suite__project__in=accessible_api_projects(self.request.user),
        ).select_related('test_suite', 'test_case__project').prefetch_related('test_case__steps')

    def perform_update(self, serializer):
        instance = self.get_object()
        with transaction.atomic():
            suite = TestSuite.objects.select_for_update().filter(pk=instance.test_suite_id).first()
            if suite is None or not can_access_api_project(suite.project, self.request.user):
                raise Http404
            row = TestSuiteCase.objects.select_for_update().get(pk=instance.pk)
            if suite.executions.filter(status='RUNNING').exists():
                raise Conflict('测试套件正在执行，不能修改成员')
            serializer.instance = row
            serializer.save()

    def perform_destroy(self, instance):
        with transaction.atomic():
            suite = TestSuite.objects.select_for_update().filter(pk=instance.test_suite_id).first()
            if suite is None or not can_access_api_project(suite.project, self.request.user):
                raise Http404
            if suite.executions.filter(status='RUNNING').exists():
                raise Conflict('测试套件正在执行，不能移除成员')
            instance.delete()


class TestSuiteCaseActionsMixin:
    """DRF actions to attach cases and atomically reorder all mixed suite members."""

    def _get_action_suite(self, pk):
        suite = TestSuite.objects.select_related('project').filter(pk=pk).first()
        if suite is None or not can_access_api_project(suite.project, self.request.user):
            raise Http404
        return suite

    @action(detail=True, methods=['post'], url_path='duplicate')
    def duplicate(self, request, pk=None):
        initial_suite = self._get_action_suite(pk)
        with transaction.atomic():
            source = TestSuite.objects.select_for_update().select_related('project').filter(
                pk=initial_suite.pk,
            ).first()
            if source is None or not can_access_api_project(source.project, request.user):
                raise Http404

            name = request.data.get('name')
            if not isinstance(name, str) or not name.strip():
                raise ValidationError({'name': '套件名称不能为空'})

            request_members = list(
                TestSuiteRequest.objects.select_for_update().filter(test_suite=source).order_by('pk')
            )
            case_members = list(
                TestSuiteCase.objects.select_for_update().filter(test_suite=source).order_by('pk')
            )

            request_ids = {member.request_id for member in request_members}
            request_locations = dict(
                ApiRequest.objects.filter(pk__in=request_ids).values_list('pk', 'collection_id')
            )
            if len(request_locations) != len(request_ids) or any(
                collection_id is None for collection_id in request_locations.values()
            ):
                raise ValidationError({'members': '测试套件成员不存在、跨项目或无权限'})

            collection_ids = set(request_locations.values())
            collections = {
                collection.pk: collection
                for collection in ApiCollection.objects.select_for_update()
                .select_related('project').filter(pk__in=collection_ids).order_by('pk')
            }
            api_requests = {
                api_request.pk: api_request
                for api_request in ApiRequest.objects.select_for_update()
                .filter(pk__in=request_ids).order_by('pk')
            }
            if len(collections) != len(collection_ids) or len(api_requests) != len(request_ids):
                raise ValidationError({'members': '测试套件成员不存在、跨项目或无权限'})
            if any(
                api_requests[member.request_id].collection_id != request_locations[member.request_id]
                or collections[request_locations[member.request_id]].project_id != source.project_id
                or not can_access_api_project(
                    collections[request_locations[member.request_id]].project, request.user,
                )
                for member in request_members
            ):
                raise ValidationError({'members': '测试套件成员不存在、跨项目或无权限'})

            case_ids = {member.test_case_id for member in case_members}
            cases = {
                test_case.pk: test_case
                for test_case in ApiTestCase.objects.select_for_update()
                .filter(pk__in=case_ids).order_by('pk')
            }
            if len(cases) != len(case_ids) or any(
                cases[member.test_case_id].project_id != source.project_id
                or not can_access_api_project(cases[member.test_case_id].project, request.user)
                for member in case_members
            ):
                raise ValidationError({'members': '测试套件成员不存在、跨项目或无权限'})

            if source.environment_id:
                Environment.objects.select_for_update().filter(pk=source.environment_id).first()

            serializer = self.get_serializer(data={
                'name': name.strip(),
                'description': source.description,
                'project': source.project_id,
                'environment': source.environment_id,
            })
            serializer.is_valid(raise_exception=True)
            self.perform_create(serializer)
            duplicate = serializer.instance

            TestSuiteRequest.objects.bulk_create([
                TestSuiteRequest(
                    test_suite=duplicate,
                    request_id=member.request_id,
                    order=member.order,
                    assertions=member.assertions,
                    enabled=member.enabled,
                )
                for member in request_members
            ])
            TestSuiteCase.objects.bulk_create([
                TestSuiteCase(
                    test_suite=duplicate,
                    test_case_id=member.test_case_id,
                    order=member.order,
                    enabled=member.enabled,
                )
                for member in case_members
            ])

        return Response(self.get_serializer(duplicate).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='add-cases')
    def add_cases(self, request, pk=None):
        initial_suite = self._get_action_suite(pk)
        case_ids = request.data.get('case_ids', [])
        if not isinstance(case_ids, list):
            raise ValidationError({'case_ids': '必须是数组'})
        try:
            case_ids = [ApiTestCase._meta.pk.to_python(case_id) for case_id in case_ids]
        except (TypeError, ValueError):
            raise ValidationError({'case_ids': '用例ID无效'})
        if len(case_ids) != len(set(case_ids)):
            raise ValidationError({'case_ids': '用例ID不能重复'})

        with transaction.atomic():
            suite = TestSuite.objects.select_for_update().select_related('project').filter(pk=initial_suite.pk).first()
            if suite is None or not can_access_api_project(suite.project, request.user):
                raise Http404
            if suite.executions.filter(status='RUNNING').exists():
                raise Conflict('测试套件正在执行，不能修改成员')
            cases = list(
                ApiTestCase.objects.select_for_update().filter(pk__in=case_ids).order_by('pk')
            )
            if len(cases) != len(case_ids) or any(case.project_id != suite.project_id for case in cases):
                raise ValidationError({'case_ids': '用例不存在、跨项目或无权限'})
            next_order = max(
                [row.order for row in suite.testsuiterequest_set.select_for_update()]
                + [row.order for row in suite.suite_cases.select_for_update()]
                + [-1]
            ) + 1
            existing = set(suite.suite_cases.values_list('test_case_id', flat=True))
            for case_id in case_ids:
                if case_id in existing:
                    continue
                TestSuiteCase.objects.create(
                    test_suite=suite,
                    test_case=next(case for case in cases if case.pk == case_id),
                    order=next_order,
                    enabled=True,
                )
                next_order += 1
        return Response({'added': [case_id for case_id in case_ids if case_id not in existing]})

    @action(detail=True, methods=['post'], url_path='reorder')
    def reorder(self, request, pk=None):
        initial_suite = self._get_action_suite(pk)
        items = request.data.get('items')
        if not isinstance(items, list):
            raise ValidationError({'items': '必须是完整成员数组'})
        normalized = []
        try:
            for item in items:
                if not isinstance(item, dict) or item.get('kind') not in {'request', 'case'}:
                    raise ValueError
                item_id = (ApiTestCase if item['kind'] == 'case' else ApiRequest)._meta.pk.to_python(item['id'])
                normalized.append((item['kind'], item_id))
        except (KeyError, TypeError, ValueError):
            raise ValidationError({'items': '每项必须包含 kind=request|case 和有效 id'})
        if len(set(normalized)) != len(normalized):
            raise ValidationError({'items': '成员不能重复'})

        with transaction.atomic():
            suite = TestSuite.objects.select_for_update().select_related('project').filter(pk=initial_suite.pk).first()
            if suite is None or not can_access_api_project(suite.project, request.user):
                raise Http404
            if suite.executions.filter(status='RUNNING').exists():
                raise Conflict('测试套件正在执行，不能调整成员顺序')
            requests = list(TestSuiteRequest.objects.select_for_update().filter(test_suite=suite).order_by('pk'))
            cases = list(TestSuiteCase.objects.select_for_update().filter(test_suite=suite).order_by('pk'))
            current = {('request', item.request_id): item for item in requests}
            current.update({('case', item.test_case_id): item for item in cases})
            if len(normalized) != len(current) or set(normalized) != set(current):
                raise ValidationError({'items': '排序必须恰好包含该套件的全部请求和接口用例'})
            for order, key in enumerate(normalized):
                current[key].order = order
                current[key].save(update_fields=['order'])
        return Response({'items': [{'kind': kind, 'id': item_id, 'order': order}
                                   for order, (kind, item_id) in enumerate(normalized)]})
