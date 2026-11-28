import pytest
from unittest.mock import patch
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


@pytest.mark.django_db
def test_revision_failure_rolls_back_case_change_and_keeps_revision_sequence():
    user = User.objects.create_user(username='revision-rollback-owner', password='pass')
    project = Project.objects.create(name='Revision rollback', owner=user)
    client = APIClient()
    client.force_authenticate(user=user)
    client.raise_request_exception = False

    with patch(
        'apps.testcases.revisions.TestCaseRevision.objects.create',
        side_effect=RuntimeError('simulated revision failure'),
    ):
        failed_create = client.post('/api/testcases/', {
            'project_id': project.pk, 'title': 'Must roll back', 'expected_result': 'ok',
        }, format='json')
    assert failed_create.status_code == 500
    assert not CaseModel.objects.filter(project=project, title='Must roll back').exists()
    assert RevisionModel.objects.count() == 0

    created = client.post('/api/testcases/', {
        'project_id': project.pk, 'title': 'Stable case', 'expected_result': 'ok',
    }, format='json')
    assert created.status_code == 201
    testcase = CaseModel.objects.get(pk=created.data['id'])
    assert RevisionModel.objects.filter(test_case=testcase).values_list(
        'revision', flat=True,
    ).get() == 1

    with patch(
        'apps.testcases.revisions.TestCaseRevision.objects.create',
        side_effect=RuntimeError('simulated revision failure'),
    ):
        failed_update = client.patch(
            f'/api/testcases/{testcase.pk}/', {'title': 'Must roll back'}, format='json',
        )
    assert failed_update.status_code == 500
    testcase.refresh_from_db()
    assert testcase.title == 'Stable case'
    assert list(RevisionModel.objects.filter(test_case=testcase).values_list('revision', flat=True)) == [1]

    recovered = client.patch(
        f'/api/testcases/{testcase.pk}/', {'title': 'Recovered update'}, format='json',
    )
    assert recovered.status_code == 200
    testcase.refresh_from_db()
    assert testcase.title == 'Recovered update'
    assert list(RevisionModel.objects.filter(test_case=testcase).values_list('revision', flat=True)) == [2, 1]
