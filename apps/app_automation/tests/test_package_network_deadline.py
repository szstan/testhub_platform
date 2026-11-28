import math

import pytest
from django.contrib.auth import get_user_model
from django.db.models.deletion import ProtectedError
from django.test import override_settings
from rest_framework.test import APIClient

from apps.app_automation.models import (
    AppPackage,
    AppProject,
    AppScheduledTask,
    AppTestCase,
)
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner
from apps.app_automation.serializers import AppTestCaseSerializer


User = get_user_model()


@pytest.mark.django_db
def test_app_packages_are_scoped_and_only_owner_or_admin_can_mutate():
    owner = User.objects.create_user(username='app-package-owner')
    member = User.objects.create_user(username='app-package-member')
    outsider = User.objects.create_user(username='app-package-outsider')
    admin = User.objects.create_user(username='app-package-admin', is_staff=True)
    project = AppProject.objects.create(name='package project', owner=owner)
    project.members.add(member)
    own = AppPackage.objects.create(
        name='Owned package', package_name='org.example.owned', created_by=owner,
    )
    foreign = AppPackage.objects.create(
        name='Foreign package', package_name='org.example.foreign', created_by=outsider,
    )
    client = APIClient()
    client.force_authenticate(user=member)

    listed = client.get('/api/app-automation/packages/')
    assert listed.status_code == 200
    listed_ids = {item['id'] for item in listed.data['results']}
    assert own.pk in listed_ids
    assert foreign.pk not in listed_ids
    assert client.get(f'/api/app-automation/packages/{foreign.pk}/').status_code == 404
    denied = client.patch(
        f'/api/app-automation/packages/{own.pk}/', {'name': 'Member edit'}, format='json',
    )
    assert denied.status_code == 403

    client.force_authenticate(user=owner)
    updated = client.patch(
        f'/api/app-automation/packages/{own.pk}/',
        {'name': 'Owner edit', 'created_by': outsider.pk},
        format='json',
    )
    assert updated.status_code == 200
    own.refresh_from_db()
    assert own.name == 'Owner edit'
    assert own.created_by_id == owner.pk

    client.force_authenticate(user=admin)
    admin_updated = client.patch(
        f'/api/app-automation/packages/{foreign.pk}/', {'name': 'Admin edit'}, format='json',
    )
    assert admin_updated.status_code == 200


@pytest.mark.django_db
def test_package_create_ignores_forged_creator_and_delete_preserves_references():
    owner = User.objects.create_user(username='app-package-create-owner')
    attacker = User.objects.create_user(username='app-package-create-attacker')
    project = AppProject.objects.create(name='package reference project', owner=owner)
    package = AppPackage.objects.create(
        name='Referenced package', package_name='org.example.referenced', created_by=owner,
    )
    case = AppTestCase.objects.create(
        project=project, name='referencing case', app_package=package, created_by=owner,
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    created = client.post('/api/app-automation/packages/', {
        'name': 'Created package',
        'package_name': 'org.example.created',
        'created_by': attacker.pk,
    }, format='json')
    assert created.status_code == 201
    assert AppPackage.objects.get(pk=created.data['id']).created_by_id == owner.pk

    denied = client.delete(f'/api/app-automation/packages/{package.pk}/')
    assert denied.status_code == 409
    assert AppPackage.objects.filter(pk=package.pk).exists()
    assert AppTestCase.objects.filter(pk=case.pk, app_package=package).exists()
    with pytest.raises(ProtectedError):
        package.delete()


@pytest.mark.django_db
def test_scheduled_task_reference_protects_app_package_from_direct_deletion():
    owner = User.objects.create_user(username='app-package-schedule-owner')
    project = AppProject.objects.create(name='package schedule project', owner=owner)
    package = AppPackage.objects.create(
        name='Scheduled package', package_name='org.example.scheduled', created_by=owner,
    )
    AppScheduledTask.objects.create(
        project=project,
        name='package schedule',
        task_type='TEST_CASE',
        trigger_type='INTERVAL',
        interval_seconds=60,
        app_package=package,
        created_by=owner,
    )

    with pytest.raises(ProtectedError):
        package.delete()
    assert AppPackage.objects.filter(pk=package.pk).exists()


def test_case_timeout_is_bounded_at_the_serializer_boundary(db):
    owner = User.objects.create_user(username='app-case-timeout-owner')
    case = AppTestCase.objects.create(name='timeout case', created_by=owner)

    accepted = AppTestCaseSerializer(instance=case, data={'timeout': 3600}, partial=True)
    rejected = AppTestCaseSerializer(instance=case, data={'timeout': 3601}, partial=True)
    assert accepted.is_valid(), accepted.errors
    assert not rejected.is_valid()
    assert 'timeout' in rejected.errors


def _runner():
    runner = UiFlowRunner.__new__(UiFlowRunner)
    runner.runtime = {'base_url': ''}
    runner._render_value = lambda value: value
    runner._sensitive_output_paths = lambda _value: []
    return runner


@override_settings(PERF_ALLOW_PRIVATE_TARGETS=True)
def test_api_request_denies_private_targets_even_if_performance_setting_allows_them():
    from unittest.mock import patch

    runner = _runner()
    with patch('apps.app_automation.runners.ui_flow_runner.guarded_request') as guarded:
        with pytest.raises(ValueError, match='URL is invalid or disallowed'):
            runner._action_api_request({'url': 'http://127.0.0.1:8080/health'})
    guarded.assert_not_called()


@pytest.mark.parametrize('timeout', [0, -1, True, 60.1, '5', math.inf, math.nan])
def test_api_request_rejects_invalid_or_unbounded_timeout(timeout):
    from unittest.mock import patch

    runner = _runner()
    with (
        patch('apps.app_automation.runners.ui_flow_runner.validate_outbound_url', side_effect=lambda url, **_kwargs: url),
        patch('apps.app_automation.runners.ui_flow_runner.guarded_request') as guarded,
        pytest.raises(ValueError, match='api_request timeout'),
    ):
        runner._action_api_request({'url': 'https://example.test/health', 'timeout': timeout})
    guarded.assert_not_called()


def test_api_request_uses_bounded_private_deny_with_guarded_transport():
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    runner = _runner()
    response = SimpleNamespace(
        status_code=200,
        headers={'Content-Type': 'text/plain'},
        text='ok',
    )
    with (
        patch('apps.app_automation.runners.ui_flow_runner.validate_outbound_url', side_effect=lambda url, **_kwargs: url),
        patch('apps.app_automation.runners.ui_flow_runner.guarded_request', return_value=response) as guarded,
    ):
        result = runner._action_api_request({
            'url': 'https://example.test/health', 'timeout': 2.5,
        })

    assert result['status_code'] == 200
    assert guarded.call_args.kwargs['allow_private'] is False
    assert guarded.call_args.kwargs['timeout'] == 2.5
