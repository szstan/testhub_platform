import pytest
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

from apps.projects.models import Project, ProjectMember
from apps.testcases.models import TestCase as ManualTestCase
from apps.testsuites.models import TestSuite as ManualTestSuite, TestSuiteCase as ManualTestSuiteCase


pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def users():
    owner = User.objects.create_user(
        username='suite-owner',
        email='suite-owner@example.com',
        password='StrongPass123',
    )
    member = User.objects.create_user(
        username='suite-member',
        email='suite-member@example.com',
        password='StrongPass123',
    )
    outsider = User.objects.create_user(
        username='suite-outsider',
        email='suite-outsider@example.com',
        password='StrongPass123',
    )
    return owner, member, outsider


@pytest.fixture
def project_data(users):
    owner, member, outsider = users
    project = Project.objects.create(
        name='Suite project',
        description='Project used by testsuite API tests',
        owner=owner,
    )
    ProjectMember.objects.create(project=project, user=member, role='tester')
    other_project = Project.objects.create(
        name='Other project',
        owner=outsider,
    )
    case_one = ManualTestCase.objects.create(
        project=project,
        title='First case',
        expected_result='First result',
        author=owner,
    )
    case_two = ManualTestCase.objects.create(
        project=project,
        title='Second case',
        expected_result='Second result',
        author=owner,
    )
    other_case = ManualTestCase.objects.create(
        project=other_project,
        title='Other project case',
        expected_result='Other result',
        author=outsider,
    )
    return {
        'project': project,
        'other_project': other_project,
        'case_one': case_one,
        'case_two': case_two,
        'other_case': other_case,
    }


def authenticate(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


def test_testsuite_requires_authentication(api_client):
    response = api_client.get('/api/testsuites/')

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_owner_can_create_list_and_update_suite(api_client, users, project_data):
    owner, _, _ = users
    project = project_data['project']
    case_one = project_data['case_one']
    case_two = project_data['case_two']
    authenticate(api_client, owner)

    response = api_client.post(
        '/api/testsuites/',
        {
            'project_id': project.id,
            'name': 'Regression suite',
            'description': 'Critical regression cases',
            'test_case_ids': [case_two.id, case_one.id],
        },
        format='json',
    )

    assert response.status_code == status.HTTP_201_CREATED
    suite = ManualTestSuite.objects.get(pk=response.data['id'])
    assert suite.author_id == owner.id
    assert suite.project_id == project.id
    assert list(
        suite.testsuitecase_set.order_by('order').values_list('testcase_id', flat=True)
    ) == [case_two.id, case_one.id]
    assert response.data['test_case_count'] == 2
    assert response.data['project']['id'] == project.id

    list_response = api_client.get('/api/testsuites/', {'project': project.id})
    assert list_response.status_code == status.HTTP_200_OK
    assert len(list_response.data['results']) == 1
    assert list_response.data['results'][0]['name'] == 'Regression suite'

    update_response = api_client.patch(
        f'/api/testsuites/{suite.id}/',
        {'name': 'Updated regression suite'},
        format='json',
    )
    assert update_response.status_code == status.HTTP_200_OK
    assert update_response.data['name'] == 'Updated regression suite'


def test_member_can_manage_suite_but_outsider_cannot_see_it(api_client, users, project_data):
    owner, member, outsider = users
    project = project_data['project']
    suite = ManualTestSuite.objects.create(
        project=project,
        name='Member suite',
        author=owner,
    )
    authenticate(api_client, member)
    member_response = api_client.get(f'/api/testsuites/{suite.id}/')
    assert member_response.status_code == status.HTTP_200_OK

    authenticate(api_client, outsider)
    outsider_list = api_client.get('/api/testsuites/')
    assert outsider_list.status_code == status.HTTP_200_OK
    assert outsider_list.data['results'] == []
    assert api_client.get(f'/api/testsuites/{suite.id}/').status_code == status.HTTP_404_NOT_FOUND


def test_create_and_add_reject_inaccessible_or_cross_project_cases(api_client, users, project_data):
    owner, _, outsider = users
    project = project_data['project']
    other_project = project_data['other_project']
    case_one = project_data['case_one']
    other_case = project_data['other_case']
    authenticate(api_client, owner)

    forbidden_project = api_client.post(
        '/api/testsuites/',
        {'project_id': other_project.id, 'name': 'Forbidden suite'},
        format='json',
    )
    assert forbidden_project.status_code == status.HTTP_403_FORBIDDEN

    suite = ManualTestSuite.objects.create(project=project, name='Cross project suite', author=owner)
    cross_project = api_client.post(
        f'/api/testsuites/{suite.id}/add_test_case/',
        {'test_case_id': other_case.id},
        format='json',
    )
    assert cross_project.status_code == status.HTTP_400_BAD_REQUEST
    assert not ManualTestSuiteCase.objects.filter(testsuite=suite).exists()

    authenticate(api_client, outsider)
    inaccessible_suite = api_client.post(
        f'/api/testsuites/{suite.id}/add_test_case/',
        {'test_case_id': case_one.id},
        format='json',
    )
    assert inaccessible_suite.status_code == status.HTTP_404_NOT_FOUND


def test_suite_case_add_remove_and_reorder(api_client, users, project_data):
    owner, _, _ = users
    suite = ManualTestSuite.objects.create(
        project=project_data['project'],
        name='Ordering suite',
        author=owner,
    )
    authenticate(api_client, owner)

    add_response = api_client.post(
        f'/api/testsuites/{suite.id}/add_test_cases/',
        {'test_case_ids': [project_data['case_one'].id, project_data['case_two'].id]},
        format='json',
    )
    assert add_response.status_code == status.HTTP_201_CREATED
    assert add_response.data['added'] == 2

    reorder_response = api_client.post(
        f'/api/testsuites/{suite.id}/update_test_case_order/',
        {
            'test_case_orders': [
                {'test_case_id': project_data['case_one'].id, 'order': 10},
                {'test_case_id': project_data['case_two'].id, 'order': 1},
            ]
        },
        format='json',
    )
    assert reorder_response.status_code == status.HTTP_200_OK
    assert list(
        suite.testsuitecase_set.order_by('order').values_list('testcase_id', flat=True)
    ) == [project_data['case_two'].id, project_data['case_one'].id]

    members_response = api_client.get(f'/api/testsuites/{suite.id}/test_cases/')
    assert members_response.status_code == status.HTTP_200_OK
    assert [item['testcase_id'] for item in members_response.data] == [
        project_data['case_two'].id,
        project_data['case_one'].id,
    ]

    remove_response = api_client.delete(
        f'/api/testsuites/{suite.id}/testcases/{project_data["case_two"].id}/'
    )
    assert remove_response.status_code == status.HTTP_200_OK
    assert not ManualTestSuiteCase.objects.filter(
        testsuite=suite, testcase_id=project_data['case_two'].id
    ).exists()


def test_delete_suite_is_limited_to_accessible_projects(api_client, users, project_data):
    owner, _, outsider = users
    suite = ManualTestSuite.objects.create(
        project=project_data['project'],
        name='Delete suite',
        author=owner,
    )
    authenticate(api_client, outsider)
    response = api_client.delete(f'/api/testsuites/{suite.id}/')

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert ManualTestSuite.objects.filter(pk=suite.id).exists()

    authenticate(api_client, owner)
    response = api_client.delete(f'/api/testsuites/{suite.id}/')
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert not ManualTestSuite.objects.filter(pk=suite.id).exists()
