import pytest
from unittest.mock import patch
from rest_framework import status
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.testcases.views import TestCaseDetailView
from apps.users.models import User


@pytest.mark.django_db
def test_patch_after_concurrent_case_delete_returns_not_found():
    owner = User.objects.create_user(username='case-lock-race-owner')
    project = Project.objects.create(name='Case lock race', owner=owner)
    testcase = TestCase.objects.create(
        project=project, title='Delete during PATCH', expected_result='pass', author=owner,
    )
    client = APIClient()
    client.force_authenticate(owner)
    client.raise_request_exception = False

    perform_update = TestCaseDetailView.perform_update

    def delete_before_locked_lookup(view, serializer):
        TestCase.objects.filter(pk=serializer.instance.pk).delete()
        return perform_update(view, serializer)

    with patch.object(TestCaseDetailView, 'perform_update', delete_before_locked_lookup):
        response = client.patch(
            f'/api/testcases/{testcase.pk}/', {'title': 'Updated'}, format='json',
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert not TestCase.objects.filter(pk=testcase.pk).exists()
