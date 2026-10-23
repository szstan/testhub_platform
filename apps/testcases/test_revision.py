import pytest
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.testcases.models import TestCase as CaseModel
from apps.executions.models import TestCaseRevision as RevisionModel
from apps.users.models import User


@pytest.mark.django_db
def test_test_case_api_records_revision_snapshots():
    user = User.objects.create_user(username='revision-owner', password='pass')
    project = Project.objects.create(name='Revision Project', owner=user)
    client = APIClient()
    client.force_authenticate(user=user)

    created = client.post(
        '/api/testcases/',
        {
            'project_id': project.id,
            'title': 'Login case',
            'description': 'initial',
            'preconditions': 'user exists',
            'steps': 'login',
            'expected_result': 'success',
            'priority': 'high',
            'test_type': 'functional',
            'tags': ['smoke'],
        },
        format='json',
    )
    assert created.status_code == 201, created.data
    testcase = CaseModel.objects.get(title='Login case', project=project)
    first = RevisionModel.objects.get(test_case=testcase, revision=1)
    assert first.snapshot['title'] == 'Login case'

    updated = client.patch(
        f'/api/testcases/{testcase.id}/',
        {'title': 'Login case updated', 'expected_result': 'success and redirect'},
        format='json',
    )
    assert updated.status_code == 200
    second = RevisionModel.objects.get(test_case=testcase, revision=2)
    assert second.snapshot['title'] == 'Login case updated'
    assert second.snapshot['expected_result'] == 'success and redirect'
    assert second.snapshot['external_ref'] == testcase.external_ref


@pytest.mark.django_db
def test_test_case_external_ref_is_stable_and_project_scoped():
    user = User.objects.create_user(username='external-ref-owner', password='pass')
    project = Project.objects.create(name='External Ref Project', owner=user)
    other_project = Project.objects.create(name='Other Project', owner=user)
    client = APIClient()
    client.force_authenticate(user=user)

    first = client.post(
        '/api/testcases/',
        {
            'project_id': project.id,
            'external_ref': 'checkout.login',
            'title': 'Login case',
            'expected_result': 'success',
        },
        format='json',
    )
    assert first.status_code == 201, first.data
    assert first.data['external_ref'] == 'checkout.login'

    duplicate = client.post(
        '/api/testcases/',
        {
            'project_id': project.id,
            'external_ref': 'checkout.login',
            'title': 'Duplicate case',
            'expected_result': 'success',
        },
        format='json',
    )
    assert duplicate.status_code == 400

    allowed_in_other_project = client.post(
        '/api/testcases/',
        {
            'project_id': other_project.id,
            'external_ref': 'checkout.login',
            'title': 'Other project case',
            'expected_result': 'success',
        },
        format='json',
    )
    assert allowed_in_other_project.status_code == 201, allowed_in_other_project.data
    assert allowed_in_other_project.data['external_ref'] == 'checkout.login'
