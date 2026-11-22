import pytest
from django.contrib.auth import get_user_model

from apps.api_testing.models import ApiProject, TestExecution as ApiTestExecution, TestSuite as ApiTestSuite
from apps.api_testing.utils import execute_test_suite


@pytest.mark.django_db
def test_background_suite_execution_rejects_a_stale_project():
    User = get_user_model()
    owner = User.objects.create_user(username='scheduled-stale-owner')
    other = User.objects.create_user(username='scheduled-stale-other')
    source = ApiProject.objects.create(
        name='scheduled source', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    target = ApiProject.objects.create(
        name='scheduled target', project_type='HTTP', status='IN_PROGRESS', owner=other,
    )
    suite = ApiTestSuite.objects.create(project=source, name='scheduled suite', created_by=owner)
    ApiTestSuite.objects.filter(pk=suite.pk).update(project=target)

    result = execute_test_suite(suite, None, owner)

    assert result['success'] is False
    assert not ApiTestExecution.objects.filter(test_suite=suite).exists()


@pytest.mark.django_db
def test_background_suite_execution_requires_access_to_current_project():
    User = get_user_model()
    owner = User.objects.create_user(username='scheduled-current-owner')
    outsider = User.objects.create_user(username='scheduled-current-outsider')
    project = ApiProject.objects.create(
        name='scheduled current', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    suite = ApiTestSuite.objects.create(project=project, name='scheduled suite', created_by=owner)

    result = execute_test_suite(suite, None, outsider)

    assert result['success'] is False
    assert not ApiTestExecution.objects.filter(test_suite=suite).exists()
