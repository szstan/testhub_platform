"""Suite request members must obey the same mutation guards as case members."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from rest_framework.test import APIClient

from apps.api_testing.models import (
    ApiCollection, ApiProject, ApiRequest, ApiTestCase, Environment,
    TestSuite as Suite, TestSuiteCase as SuiteCase,
    TestSuiteRequest as Member, TestExecution as Execution,
)
from apps.api_testing.serializers import TestSuiteRequestSerializer as MemberSerializer
from apps.api_testing.test_execution_integration import graph
from apps.api_testing.views import TestSuiteRequestViewSet as MemberViewSet, TestSuiteViewSet as SuiteViewSet


@pytest.fixture
def member(graph):
    suite = Suite.objects.create(project=graph.project, name='Member guard suite', created_by=graph.owner)
    row = Member.objects.create(test_suite=suite, request=graph.definition)
    return SimpleNamespace(suite=suite, row=row)


@pytest.fixture(params=['owner', 'staff'])
def actor(graph, request):
    user = graph.owner
    if request.param == 'staff':
        user = get_user_model().objects.create_user(username='member-guard-admin', is_staff=True)
    graph.client.force_authenticate(user=user)
    return user


@pytest.mark.parametrize('method', ['patch', 'delete'])
def test_running_suite_rejects_request_member_mutation(graph, member, actor, method):
    Execution.objects.create(test_suite=member.suite, status='RUNNING', executed_by=graph.owner)
    path = f'/api/api-testing/test-suite-requests/{member.row.pk}/'
    result = graph.client.patch(path, {'enabled': False}, format='json') if method == 'patch' else graph.client.delete(path)
    assert result.status_code == 409, result.data
    member.row.refresh_from_db()
    assert member.row.enabled is True


def test_running_suite_rejects_request_insertion(graph, member, actor):
    Execution.objects.create(test_suite=member.suite, status='RUNNING', executed_by=graph.owner)
    result = graph.client.post(f'/api/api-testing/test-suites/{member.suite.pk}/add-requests/', {
        'request_ids': [graph.definition.pk],
    }, format='json')
    assert result.status_code == 409, result.data
    assert member.suite.testsuiterequest_set.count() == 1


@pytest.mark.parametrize('invalid', [
    'not an array', [{'type': 'javascript', 'expected': True}],
    [{'type': 'json_path', 'expected': 1}], [{'type': 'header', 'expected': 'x'}],
])
def test_request_member_rejects_invalid_assertions_atomically(graph, member, invalid):
    result = graph.client.patch(f'/api/api-testing/test-suite-requests/{member.row.pk}/', {
        'enabled': False, 'assertions': invalid,
    }, format='json')
    assert result.status_code == 400, result.data
    member.row.refresh_from_db()
    assert member.row.enabled is True
    assert member.row.assertions == []


def test_request_member_keeps_legacy_status_value_assertions(graph, member):
    assertions = [{'type': 'status_code', 'value': 200}]
    result = graph.client.patch(f'/api/api-testing/test-suite-requests/{member.row.pk}/', {
        'assertions': assertions,
    }, format='json')
    assert result.status_code == 200, result.data
    member.row.refresh_from_db()
    assert member.row.assertions == assertions


def foreign_project():
    outsider = get_user_model().objects.create_user(username='member-guard-outsider')
    return ApiProject.objects.create(name='Private new project', project_type='HTTP', status='IN_PROGRESS', owner=outsider)


def test_request_member_update_rechecks_project_access_after_validation(graph, member):
    target = foreign_project()
    original = MemberSerializer.is_valid

    def revoke_after_validation(serializer, *args, **kwargs):
        valid = original(serializer, *args, **kwargs)
        if valid:
            Suite.objects.filter(pk=member.suite.pk).update(project=target)
        return valid

    with patch.object(MemberSerializer, 'is_valid', new=revoke_after_validation):
        result = graph.client.patch(f'/api/api-testing/test-suite-requests/{member.row.pk}/', {
            'enabled': False,
        }, format='json')
    assert result.status_code == 404, result.data
    member.row.refresh_from_db()
    assert member.row.enabled is True


def test_request_member_delete_rechecks_project_access_after_lookup(graph, member):
    target = foreign_project()
    original = MemberViewSet.get_object

    def revoke_after_lookup(view):
        row = original(view)
        Suite.objects.filter(pk=member.suite.pk).update(project=target)
        return row

    with patch.object(MemberViewSet, 'get_object', new=revoke_after_lookup):
        result = graph.client.delete(f'/api/api-testing/test-suite-requests/{member.row.pk}/')
    assert result.status_code == 404, result.data
    assert Member.objects.filter(pk=member.row.pk).exists()


def test_direct_request_member_creation_is_not_a_supported_route(graph):
    result = graph.client.post('/api/api-testing/test-suite-requests/', {}, format='json')
    assert result.status_code == 405


def test_duplicate_suite_copies_mixed_membership_state_and_environment(graph):
    environment = Environment.objects.create(
        name='QA environment', scope='LOCAL', variables={'base_url': 'https://qa.example.test'},
        project=graph.project, created_by=graph.owner,
    )
    suite = Suite.objects.create(
        project=graph.project, name='Source suite', description='Original description',
        environment=environment, created_by=graph.owner,
    )
    second_request = ApiRequest.objects.create(
        name='Logout', collection=graph.collection, method='POST',
        url='https://example.test/logout', created_by=graph.owner,
    )
    case = ApiTestCase.objects.create(project=graph.project, name='Negative login', created_by=graph.owner)
    Member.objects.create(
        test_suite=suite, request=graph.definition, order=8, enabled=False,
        assertions=[{'type': 'status_code', 'value': 401}],
    )
    Member.objects.create(test_suite=suite, request=second_request, order=12, enabled=True, assertions=[])
    SuiteCase.objects.create(test_suite=suite, test_case=case, order=3, enabled=False)
    copied_name = '复制' + 'x' * 198

    result = graph.client.post(
        f'/api/api-testing/test-suites/{suite.pk}/duplicate/', {'name': copied_name}, format='json',
    )

    assert result.status_code == 201, result.data
    duplicate = Suite.objects.get(pk=result.data['id'])
    assert duplicate.name == copied_name
    assert duplicate.description == suite.description
    assert duplicate.project_id == suite.project_id
    assert duplicate.environment_id == environment.pk
    assert duplicate.created_by_id == graph.owner.pk
    assert list(duplicate.testsuiterequest_set.order_by('order').values_list(
        'request_id', 'order', 'enabled', 'assertions',
    )) == [
        (graph.definition.pk, 8, False, [{'type': 'status_code', 'value': 401}]),
        (second_request.pk, 12, True, []),
    ]
    assert list(duplicate.suite_cases.values_list('test_case_id', 'order', 'enabled')) == [
        (case.pk, 3, False),
    ]
    assert Suite.objects.count() == 2


@pytest.mark.parametrize('member_kind', ['request', 'case'])
def test_duplicate_rejects_cross_project_member_without_disclosing_or_persisting_it(graph, member_kind):
    suite = Suite.objects.create(project=graph.project, name='Source suite', created_by=graph.owner)
    Member.objects.create(test_suite=suite, request=graph.definition, order=0)
    outsider = get_user_model().objects.create_user(username=f'private-{member_kind}-owner')
    foreign_project = ApiProject.objects.create(
        name='Private project', project_type='HTTP', status='IN_PROGRESS', owner=outsider,
    )
    if member_kind == 'request':
        collection = ApiCollection.objects.create(project=foreign_project, name='Private collection')
        foreign_request = ApiRequest.objects.create(
            name='secret request name', collection=collection, url='https://private.example.test',
            created_by=outsider,
        )
        Member.objects.create(test_suite=suite, request=foreign_request, order=1)
    else:
        foreign_case = ApiTestCase.objects.create(
            project=foreign_project, name='secret case name', created_by=outsider,
        )
        SuiteCase.objects.create(test_suite=suite, test_case=foreign_case, order=1)

    result = graph.client.post(
        f'/api/api-testing/test-suites/{suite.pk}/duplicate/', {'name': 'Localized copy'}, format='json',
    )

    assert result.status_code == 400, result.data
    assert 'secret' not in str(result.data)
    assert Suite.objects.count() == 1
    assert Member.objects.filter(test_suite=suite).count() == (2 if member_kind == 'request' else 1)
    assert SuiteCase.objects.filter(test_suite=suite).count() == (1 if member_kind == 'case' else 0)


def test_duplicate_rechecks_source_project_access_after_initial_lookup(graph):
    suite = Suite.objects.create(project=graph.project, name='Source suite', created_by=graph.owner)
    hidden_project = foreign_project()
    original = SuiteViewSet._get_action_suite

    def move_after_lookup(view, pk):
        initial = original(view, pk)
        Suite.objects.filter(pk=pk).update(project=hidden_project)
        return initial

    with patch.object(SuiteViewSet, '_get_action_suite', new=move_after_lookup):
        result = graph.client.post(
            f'/api/api-testing/test-suites/{suite.pk}/duplicate/', {'name': 'Localized copy'}, format='json',
        )

    assert result.status_code == 404, result.data
    assert Suite.objects.count() == 1
    assert not Suite.objects.filter(name='Localized copy').exists()


def test_duplicate_database_error_rolls_back_suite_and_prior_member_inserts(graph):
    suite = Suite.objects.create(project=graph.project, name='Source suite', created_by=graph.owner)
    case = ApiTestCase.objects.create(project=graph.project, name='Case', created_by=graph.owner)
    Member.objects.create(test_suite=suite, request=graph.definition)
    SuiteCase.objects.create(test_suite=suite, test_case=case, order=1)
    client = APIClient(raise_request_exception=False)
    client.force_authenticate(user=graph.owner)

    with patch('apps.api_testing.case_views.TestSuiteCase.objects.bulk_create', side_effect=IntegrityError('simulated insert failure')):
        result = client.post(
            f'/api/api-testing/test-suites/{suite.pk}/duplicate/', {'name': 'Failed copy'}, format='json',
        )

    assert result.status_code == 500
    assert Suite.objects.count() == 1
    assert Member.objects.filter(test_suite=suite).count() == 1
    assert SuiteCase.objects.filter(test_suite=suite).count() == 1
    assert not Suite.objects.filter(name='Failed copy').exists()


def test_duplicate_rejects_invalid_name_without_creating_suite(graph):
    suite = Suite.objects.create(project=graph.project, name='Source suite', created_by=graph.owner)

    result = graph.client.post(
        f'/api/api-testing/test-suites/{suite.pk}/duplicate/', {'name': 'x' * 201}, format='json',
    )

    assert result.status_code == 400, result.data
    assert Suite.objects.count() == 1
