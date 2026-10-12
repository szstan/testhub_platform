import pytest
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

from apps.executions.models import TestPlan as ManualTestPlan, TestRun as ManualTestRun, TestRunCase as ManualTestRunCase
from apps.projects.models import Project, ProjectMember
from apps.reports.models import TestReport as ManualTestReport
from apps.reviews.models import TestCaseReview as ManualTestCaseReview
from apps.testcases.models import TestCase as ManualTestCase
from apps.versions.models import Version as ManualVersion


pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def core_data():
    owner = User.objects.create_user(
        username='core-owner',
        email='core-owner@example.com',
        password='StrongPass123',
    )
    member = User.objects.create_user(
        username='core-member',
        email='core-member@example.com',
        password='StrongPass123',
    )
    outsider = User.objects.create_user(
        username='core-outsider',
        email='core-outsider@example.com',
        password='StrongPass123',
    )
    project = Project.objects.create(name='Core project', owner=owner)
    ProjectMember.objects.create(project=project, user=member, role='tester')
    other_project = Project.objects.create(name='Other core project', owner=outsider)
    version = ManualVersion.objects.create(name='1.0.0', created_by=owner)
    version.projects.add(project)
    testcase = ManualTestCase.objects.create(
        project=project,
        title='Core smoke case',
        expected_result='The workflow succeeds',
        author=owner,
        status='active',
    )
    other_case = ManualTestCase.objects.create(
        project=other_project,
        title='Other smoke case',
        expected_result='Other workflow succeeds',
        author=outsider,
        status='active',
    )
    return {
        'owner': owner,
        'member': member,
        'outsider': outsider,
        'project': project,
        'other_project': other_project,
        'version': version,
        'testcase': testcase,
        'other_case': other_case,
    }


def auth(client, user):
    client.force_authenticate(user=user)
    return client


def results(response):
    return response.data.get('results', response.data) if isinstance(response.data, dict) else response.data


def test_project_list_is_scoped_to_owner_or_member(api_client, core_data):
    project = core_data['project']
    other_project = core_data['other_project']

    auth(api_client, core_data['member'])
    member_projects = results(api_client.get('/api/projects/'))
    assert any(item['id'] == project.id for item in member_projects)
    assert all(item['id'] != other_project.id for item in member_projects)

    auth(api_client, core_data['outsider'])
    outsider_projects = results(api_client.get('/api/projects/'))
    assert any(item['id'] == other_project.id for item in outsider_projects)
    assert all(item['id'] != project.id for item in outsider_projects)


def test_versions_and_testcases_are_scoped_to_project_members(api_client, core_data):
    owner = core_data['owner']
    member = core_data['member']
    outsider = core_data['outsider']
    project = core_data['project']
    version = core_data['version']

    auth(api_client, owner)
    version_response = api_client.get('/api/versions/')
    assert version_response.status_code == status.HTTP_200_OK
    assert any(item['id'] == version.id for item in results(version_response))

    auth(api_client, member)
    testcase_response = api_client.get('/api/testcases/', {'project': project.id})
    assert testcase_response.status_code == status.HTTP_200_OK
    assert any(item['id'] == core_data['testcase'].id for item in results(testcase_response))

    auth(api_client, outsider)
    assert results(api_client.get('/api/versions/')) == []
    outsider_testcases = results(api_client.get('/api/testcases/'))
    assert all(item['id'] != core_data['testcase'].id for item in outsider_testcases)


def test_execution_plan_creates_run_and_report_dashboard_is_scoped(api_client, core_data):
    owner = core_data['owner']
    project = core_data['project']
    testcase = core_data['testcase']
    auth(api_client, owner)

    plan_response = api_client.post(
        '/api/executions/plans/',
        {
            'name': 'Core regression plan',
            'projects': [project.id],
            'testcases': [testcase.id],
            'version': core_data['version'].id,
        },
        format='json',
    )
    assert plan_response.status_code == status.HTTP_201_CREATED

    plan = ManualTestPlan.objects.get(name='Core regression plan')
    run = ManualTestRun.objects.get(test_plan=plan, project=project)
    run_case = ManualTestRunCase.objects.get(test_run=run, testcase=testcase)
    assert plan.creator_id == owner.id
    assert run_case.status == 'untested'

    dashboard_response = api_client.get('/api/reports/reports/dashboard/', {'project': project.id})
    assert dashboard_response.status_code == status.HTTP_200_OK
    assert dashboard_response.data['active_plans'] == 1
    assert dashboard_response.data['total_cases'] == 1

    update_response = api_client.patch(
        f'/api/executions/run_cases/{run_case.id}/update_status/',
        {'status': 'passed', 'actual_result': 'Passed'},
        format='json',
    )
    assert update_response.status_code == status.HTTP_200_OK
    assert run_case.history.count() == 1

    distribution_response = api_client.get(
        '/api/reports/reports/status_distribution/',
        {'project': project.id},
    )
    assert distribution_response.status_code == status.HTTP_200_OK
    assert distribution_response.data['passed'] == 1
    assert distribution_response.data['untested'] == 0

    auth(api_client, core_data['outsider'])
    outsider_dashboard = api_client.get(
        '/api/reports/reports/dashboard/',
        {'project': project.id},
    )
    assert outsider_dashboard.status_code == status.HTTP_200_OK
    assert outsider_dashboard.data['active_plans'] == 0
    assert outsider_dashboard.data['total_cases'] == 0
    assert results(api_client.get('/api/executions/runs/')) == []
    assert api_client.get(f'/api/executions/run_cases/{run_case.id}/').status_code == status.HTTP_404_NOT_FOUND

    unauthorized_plan = api_client.post(
        '/api/executions/plans/',
        {
            'name': 'Unauthorized plan',
            'projects': [project.id],
            'testcases': [testcase.id],
        },
        format='json',
    )
    assert unauthorized_plan.status_code == status.HTTP_403_FORBIDDEN
    assert not ManualTestPlan.objects.filter(name='Unauthorized plan').exists()


def test_review_creation_and_submission_stays_with_project_members(api_client, core_data):
    owner = core_data['owner']
    member = core_data['member']
    project = core_data['project']
    testcase = core_data['testcase']
    auth(api_client, owner)

    create_response = api_client.post(
        '/api/reviews/reviews/',
        {
            'title': 'Core review',
            'description': 'Review the smoke case',
            'projects': [project.id],
            'testcases': [testcase.id],
            'reviewers': [member.id],
            'priority': 'high',
        },
        format='json',
    )
    assert create_response.status_code == status.HTTP_201_CREATED
    review = ManualTestCaseReview.objects.get(title='Core review')
    assert review.creator_id == owner.id
    assert review.projects.filter(pk=project.id).exists()

    auth(api_client, core_data['outsider'])
    outsider_list = api_client.get('/api/reviews/reviews/', {'project': project.id})
    assert outsider_list.status_code == status.HTTP_200_OK
    assert results(outsider_list) == []

    auth(api_client, member)
    submit_response = api_client.post(
        f'/api/reviews/reviews/{review.id}/submit_review/',
        {'status': 'approved', 'comment': 'Looks good'},
        format='json',
    )
    assert submit_response.status_code == status.HTTP_200_OK
    review.refresh_from_db()
    assert review.status == 'approved'
