from django.db import models, transaction
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, generics, permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.projects.models import Project
from apps.testcases.models import TestCase

from .models import TestSuite, TestSuiteCase
from .serializers import (
    TestSuiteCaseSerializer,
    TestSuiteCreateSerializer,
    TestSuiteSerializer,
    TestSuiteUpdateSerializer,
)


def get_user_accessible_projects(user):
    """Return projects the user owns or has joined."""

    return Project.objects.filter(
        models.Q(owner=user) | models.Q(members=user)
    ).distinct()


def get_user_accessible_suites(user):
    return TestSuite.objects.filter(
        project__in=get_user_accessible_projects(user)
    ).select_related('project', 'author').prefetch_related(
        'testcases', 'testsuitecase_set__testcase'
    )


def _value_from_request(data, *names):
    for name in names:
        if name in data:
            value = data.get(name)
            if isinstance(value, dict):
                value = value.get('id')
            if value not in (None, ''):
                return value
    return None


def _project_id_from_request(data):
    return _value_from_request(data, 'project_id', 'project')


def _parse_id(value, field_name):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field_name: '必须是有效的整数 ID'})
    if parsed <= 0:
        raise ValidationError({field_name: '必须是正整数 ID'})
    return parsed


def _parse_nonnegative_int(value, field_name):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValidationError({field_name: '必须是非负整数'})
    if parsed < 0:
        raise ValidationError({field_name: '必须是非负整数'})
    return parsed


def _case_id_from_request(data):
    value = _value_from_request(data, 'test_case_id', 'testcase_id')
    if value is None:
        raise ValidationError({'test_case_id': '请提供测试用例 ID'})
    return _parse_id(value, 'test_case_id')


def _case_ids_from_request(data):
    values = None
    for name in ('test_case_ids', 'testcase_ids', 'testcases'):
        if name in data:
            values = data.get(name)
            break
    if values is None:
        raise ValidationError({'test_case_ids': '请提供测试用例 ID 列表'})
    if not isinstance(values, (list, tuple)):
        values = [values]

    parsed = []
    for value in values:
        if isinstance(value, dict):
            value = value.get('test_case_id', value.get('testcase_id', value.get('id')))
        parsed.append(_parse_id(value, 'test_case_ids'))
    if len(parsed) != len(set(parsed)):
        raise ValidationError({'test_case_ids': '测试用例不能重复'})
    return parsed


def _case_for_suite(suite, testcase_id):
    testcase = TestCase.objects.filter(
        pk=testcase_id, project_id=suite.project_id
    ).first()
    if testcase is None:
        raise ValidationError({'test_case_id': '测试用例不存在或不属于该项目'})
    return testcase


def _validate_cases_for_suite(suite, testcase_ids):
    cases = list(TestCase.objects.filter(
        pk__in=testcase_ids, project_id=suite.project_id
    ))
    found = {case.id for case in cases}
    missing = [case_id for case_id in testcase_ids if case_id not in found]
    if missing:
        raise ValidationError({
            'test_case_ids': f'测试用例不存在或不属于该项目: {missing}'
        })
    return {case.id: case for case in cases}


def _serialize_suite_cases(suite):
    rows = suite.testsuitecase_set.select_related('testcase').order_by('order', 'id')
    return TestSuiteCaseSerializer(rows, many=True).data


class TestSuiteListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['project']
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'updated_at', 'name']
    ordering = ['-created_at']

    def get_queryset(self):
        return get_user_accessible_suites(self.request.user)

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return TestSuiteCreateSerializer
        return TestSuiteSerializer

    def perform_create(self, serializer):
        project_id = _project_id_from_request(self.request.data)
        if project_id is None:
            raise ValidationError({'project_id': '请提供项目 ID'})
        project_id = _parse_id(project_id, 'project_id')
        project = get_user_accessible_projects(self.request.user).filter(
            pk=project_id
        ).first()
        if project is None:
            raise PermissionDenied('项目不存在或无权限访问')

        requested_case_ids = None
        if any(name in self.request.data for name in ('test_case_ids', 'testcase_ids', 'testcases')):
            requested_case_ids = _case_ids_from_request(self.request.data)

        with transaction.atomic():
            serializer.save(author=self.request.user, project=project)
            if requested_case_ids is not None:
                cases = _validate_cases_for_suite(serializer.instance, requested_case_ids)
                TestSuiteCase.objects.bulk_create([
                    TestSuiteCase(
                        testsuite=serializer.instance,
                        testcase=cases[testcase_id],
                        order=index,
                    )
                    for index, testcase_id in enumerate(requested_case_ids)
                ])

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        output = TestSuiteSerializer(
            serializer.instance, context=self.get_serializer_context()
        )
        return Response(output.data, status=status.HTTP_201_CREATED)


class TestSuiteDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return get_user_accessible_suites(self.request.user)

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH'):
            return TestSuiteUpdateSerializer
        return TestSuiteSerializer

    def perform_update(self, serializer):
        suite = serializer.instance
        project = suite.project
        requested_project_id = _project_id_from_request(self.request.data)
        if requested_project_id is not None:
            requested_project_id = _parse_id(requested_project_id, 'project_id')
            project = get_user_accessible_projects(self.request.user).filter(
                pk=requested_project_id
            ).first()
            if project is None:
                raise PermissionDenied('项目不存在或无权限访问')

        requested_case_ids = None
        if any(name in self.request.data for name in ('test_case_ids', 'testcase_ids', 'testcases')):
            requested_case_ids = _case_ids_from_request(self.request.data)
            # Validate against the target project before changing the suite.
            probe = type('_SuiteProjectProbe', (), {
                'project_id': project.id,
            })()
            cases = _validate_cases_for_suite(probe, requested_case_ids)
        else:
            cases = None

        if project.id != suite.project_id:
            existing_case_ids = suite.testsuitecase_set.values_list(
                'testcase_id', flat=True
            )
            if TestCase.objects.filter(
                pk__in=existing_case_ids
            ).exclude(project_id=project.id).exists():
                raise ValidationError('套件中的用例必须属于目标项目')

        with transaction.atomic():
            serializer.save(project=project)
            if requested_case_ids is not None:
                TestSuiteCase.objects.filter(testsuite=serializer.instance).delete()
                TestSuiteCase.objects.bulk_create([
                    TestSuiteCase(
                        testsuite=serializer.instance,
                        testcase=cases[testcase_id],
                        order=index,
                    )
                    for index, testcase_id in enumerate(requested_case_ids)
                ])

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        instance.refresh_from_db()
        output = TestSuiteSerializer(instance, context=self.get_serializer_context())
        return Response(output.data)


class SuiteCasesMixin:
    permission_classes = [permissions.IsAuthenticated]

    def get_suite(self, pk, request=None):
        request = request or self.request
        suite = get_user_accessible_suites(request.user).filter(pk=pk).first()
        if suite is None:
            raise NotFound('测试套件不存在')
        return suite

    def _add_case(self, request, suite, testcase_id=None, order=None):
        if testcase_id is None:
            testcase_id = _case_id_from_request(request.data)
        testcase_id = _parse_id(testcase_id, 'test_case_id')
        testcase = _case_for_suite(suite, testcase_id)
        if TestSuiteCase.objects.filter(testsuite=suite, testcase=testcase).exists():
            raise ValidationError({'test_case_id': '测试用例已在该套件中'})

        if order is None:
            last_order = suite.testsuitecase_set.order_by('-order', '-id').values_list(
                'order', flat=True
            ).first()
            order = 0 if last_order is None else last_order + 1
        order = _parse_nonnegative_int(order, 'order')
        return TestSuiteCase.objects.create(
            testsuite=suite, testcase=testcase, order=order
        )


class TestSuiteCaseListView(SuiteCasesMixin, APIView):
    """Read suite members, and accept a single-case POST as a convenience."""

    def get(self, request, pk):
        suite = self.get_suite(pk)
        return Response(_serialize_suite_cases(suite))

    def post(self, request, pk):
        suite = self.get_suite(pk)
        association = self._add_case(request, suite)
        return Response(
            TestSuiteCaseSerializer(association).data,
            status=status.HTTP_201_CREATED,
        )


class TestSuiteAddCaseView(SuiteCasesMixin, APIView):
    def post(self, request, pk):
        suite = self.get_suite(pk)
        association = self._add_case(request, suite)
        return Response(
            TestSuiteCaseSerializer(association).data,
            status=status.HTTP_201_CREATED,
        )


class TestSuiteAddCasesView(SuiteCasesMixin, APIView):
    def post(self, request, pk):
        suite = self.get_suite(pk)
        testcase_ids = _case_ids_from_request(request.data)
        cases = _validate_cases_for_suite(suite, testcase_ids)
        existing_ids = set(suite.testsuitecase_set.values_list('testcase_id', flat=True))
        last_order = suite.testsuitecase_set.order_by('-order', '-id').values_list(
            'order', flat=True
        ).first()
        next_order = 0 if last_order is None else last_order + 1

        with transaction.atomic():
            associations = []
            for testcase_id in testcase_ids:
                if testcase_id in existing_ids:
                    continue
                associations.append(TestSuiteCase.objects.create(
                    testsuite=suite,
                    testcase=cases[testcase_id],
                    order=next_order,
                ))
                next_order += 1

        return Response({
            'success': True,
            'added': len(associations),
            'testcases': TestSuiteCaseSerializer(associations, many=True).data,
        }, status=status.HTTP_201_CREATED)


class TestSuiteRemoveCaseView(SuiteCasesMixin, APIView):
    def _remove(self, request, pk, testcase_id=None):
        suite = self.get_suite(pk, request=request)
        if testcase_id is None:
            testcase_id = _case_id_from_request(request.data)
        testcase_id = _parse_id(testcase_id, 'test_case_id')
        association = suite.testsuitecase_set.filter(testcase_id=testcase_id).first()
        if association is None:
            raise NotFound('用例不在该套件中')
        association.delete()
        return Response({'success': True, 'deleted': True, 'test_case_id': testcase_id})

    def post(self, request, pk):
        return self._remove(request, pk)

    def delete(self, request, pk, testcase_id=None):
        return self._remove(request, pk, testcase_id)


class TestSuiteReorderCasesView(SuiteCasesMixin, APIView):
    def post(self, request, pk):
        suite = self.get_suite(pk)
        items = request.data.get('test_case_orders', request.data.get('testcase_orders'))
        if items is None:
            raise ValidationError({'test_case_orders': '请提供用例顺序列表'})
        if not isinstance(items, (list, tuple)):
            raise ValidationError({'test_case_orders': '必须是列表'})

        updates = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValidationError({'test_case_orders': '每项必须包含 test_case_id 和 order'})
            testcase_id = item.get('test_case_id', item.get('testcase_id'))
            order = item.get('order')
            testcase_id = _parse_id(testcase_id, 'test_case_id')
            if testcase_id in seen:
                raise ValidationError({'test_case_orders': '测试用例不能重复'})
            seen.add(testcase_id)
            if order is None:
                raise ValidationError({'test_case_orders': '每项都必须包含 order'})
            order = _parse_nonnegative_int(order, 'order')
            updates.append((testcase_id, order))

        association_map = {
            row.testcase_id: row
            for row in suite.testsuitecase_set.filter(testcase_id__in=seen)
        }
        missing = [
            testcase_id for testcase_id, _ in updates
            if testcase_id not in association_map
        ]
        if missing:
            raise ValidationError({
                'test_case_orders': f'用例不在该套件中: {missing}'
            })

        with transaction.atomic():
            for testcase_id, order in updates:
                association_map[testcase_id].order = order
                association_map[testcase_id].save(update_fields=['order'])

        return Response({
            'success': True,
            'updated': len(updates),
            'testcases': _serialize_suite_cases(suite),
        })


class TestSuiteCaseDetailView(SuiteCasesMixin, APIView):
    """REST-friendly alias for deleting one membership by URL."""

    def delete(self, request, pk, testcase_id):
        suite = self.get_suite(pk)
        association = suite.testsuitecase_set.filter(testcase_id=testcase_id).first()
        if association is None:
            raise NotFound('用例不在该套件中')
        association.delete()
        return Response({'success': True, 'deleted': True, 'test_case_id': testcase_id})
