import pytest
from django.db.models.query import QuerySet
from rest_framework.test import APIClient

from apps.requirement_analysis.models import TestCaseGenerationTask as GenerationTask
from apps.users.models import User


pytestmark = pytest.mark.django_db


def create_failed_task(user):
    return GenerationTask.objects.create(
        task_id='TASK_FAILED_PROGRESS',
        title='Failed generation',
        requirement_text='A requirement that could not be generated.',
        status='failed',
        progress=30,
        error_message='Writer API request timed out',
        created_by=user,
    )


def test_failed_progress_returns_error_message_without_row_lock(monkeypatch):
    user = User.objects.create_user(username='generation-owner', password='StrongPass123')
    task = create_failed_task(user)
    client = APIClient()
    client.force_authenticate(user=user)
    original_select_for_update = QuerySet.select_for_update

    def reject_generation_task_lock(queryset, *args, **kwargs):
        if queryset.model is GenerationTask:
            raise AssertionError('Progress GET must not lock the generation task row')
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, 'select_for_update', reject_generation_task_lock)
    response = client.get(
        f'/api/requirement-analysis/testcase-generation/{task.task_id}/progress/'
    )

    assert response.status_code == 200
    assert response.json() == {
        'task_id': task.task_id,
        'status': 'failed',
        'progress': 30,
        'generated_test_cases': '',
        'review_feedback': '',
        'final_test_cases': '',
        'error_message': 'Writer API request timed out',
        'completed_at': None,
    }


def test_generation_progress_requires_authentication_and_scopes_error_details():
    owner = User.objects.create_user(username='generation-owner-auth', password='StrongPass123')
    outsider = User.objects.create_user(username='generation-outsider', password='StrongPass123')
    task = create_failed_task(owner)
    path = f'/api/requirement-analysis/testcase-generation/{task.task_id}/progress/'

    client = APIClient()
    assert client.get(path).status_code == 401

    client.force_authenticate(user=outsider)
    response = client.get(path)
    assert response.status_code >= 400
    assert 'error_message' not in response.json()
    assert task.error_message not in response.content.decode('utf-8')
