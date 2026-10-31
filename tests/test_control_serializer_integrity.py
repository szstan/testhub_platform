from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import PermissionDenied

from apps.executions.models import (
    ExecutionAttempt,
    ExecutionJob,
    ExecutionNotificationEvent,
    ExecutionNotificationSubscription,
    ExecutionProfile,
)
from apps.executions.serializers import (
    ExecutionJobSerializer,
    ExecutionNotificationEventSerializer,
    ExecutionProfileSerializer,
)
from apps.projects.models import Project
from apps.users.models import User
from apps.versions.models import Version


@pytest.fixture
def profile_objects(db):
    owner = User.objects.create_user(username='serializer-owner', password='pass')
    outsider = User.objects.create_user(username='serializer-outsider', password='pass')
    project_a = Project.objects.create(name='Serializer Project A', owner=owner)
    project_b = Project.objects.create(name='Serializer Project B', owner=owner)
    project_private = Project.objects.create(name='Private Serializer Project', owner=outsider)

    version_a = Version.objects.create(name='serializer-a', created_by=owner)
    version_a.projects.add(project_a)
    version_b = Version.objects.create(name='serializer-b', created_by=owner)
    version_b.projects.add(project_b)
    shared_version = Version.objects.create(name='serializer-shared', created_by=owner)
    shared_version.projects.add(project_a, project_b)

    profile = ExecutionProfile.objects.create(
        project=project_a,
        version=version_a,
        name='serializer-profile',
        created_by=owner,
    )
    return SimpleNamespace(
        owner=owner,
        outsider=outsider,
        project_a=project_a,
        project_b=project_b,
        project_private=project_private,
        version_a=version_a,
        version_b=version_b,
        shared_version=shared_version,
        profile=profile,
    )


def profile_serializer(user, data, *, instance=None, partial=False):
    return ExecutionProfileSerializer(
        instance=instance,
        data=data,
        partial=partial,
        context={'request': SimpleNamespace(user=user)},
    )


@pytest.mark.django_db
def test_profile_serializer_validates_effective_project_and_version(profile_objects):
    data = profile_objects
    created = profile_serializer(
        data.owner,
        {'project': data.project_a.pk, 'version': data.version_a.pk, 'name': 'created-profile'},
    )
    assert created.is_valid(), created.errors

    project_only = profile_serializer(
        data.owner,
        {'project': data.project_b.pk},
        instance=data.profile,
        partial=True,
    )
    assert not project_only.is_valid()
    assert 'version' in project_only.errors

    version_only = profile_serializer(
        data.owner,
        {'version': data.shared_version.pk},
        instance=data.profile,
        partial=True,
    )
    assert version_only.is_valid(), version_only.errors

    valid_project_only = ExecutionProfile.objects.create(
        project=data.project_a,
        version=data.shared_version,
        name='shared-version-profile',
        created_by=data.owner,
    )
    project_patch = profile_serializer(
        data.owner,
        {'project': data.project_b.pk},
        instance=valid_project_only,
        partial=True,
    )
    assert project_patch.is_valid(), project_patch.errors

    put_with_effective_old_version = profile_serializer(
        data.owner,
        {'project': data.project_b.pk, 'name': data.profile.name},
        instance=data.profile,
    )
    assert not put_with_effective_old_version.is_valid()
    assert 'version' in put_with_effective_old_version.errors

    valid_combination = profile_serializer(
        data.owner,
        {'project': data.project_b.pk, 'version': data.version_b.pk, 'name': 'moved-profile'},
        instance=data.profile,
        partial=True,
    )
    assert valid_combination.is_valid(), valid_combination.errors


@pytest.mark.django_db
def test_profile_serializer_rejects_inaccessible_effective_project(profile_objects):
    data = profile_objects
    serializer = profile_serializer(
        data.outsider,
        {'project': data.project_a.pk, 'version': data.version_a.pk, 'name': 'leaked-profile'},
    )
    with pytest.raises(PermissionDenied):
        serializer.is_valid()


@pytest.fixture
def execution_job(db):
    user = User.objects.create_user(username='job-serializer-owner', password='pass')
    project = Project.objects.create(name='Job Serializer Project', owner=user)
    job = ExecutionJob.objects.create(
        project=project,
        source_type='api',
        source_id='api:suite:42',
        requested_by=user,
    )
    return user, project, job


def supported_executor():
    return SimpleNamespace(dispatcher=SimpleNamespace(supports=lambda _source_type: True))


@pytest.mark.django_db
def test_job_serializer_uses_injected_counts_and_binding_status_without_queries(execution_job):
    _user, project, job = execution_job
    job._attempts_count = 7
    serializer = ExecutionJobSerializer(
        instance=job,
        context={
            'source_binding_statuses': {(project.pk, 'api', 'api:suite:42'): True},
        },
    )

    with patch('apps.executions.adapters.get_executor_adapter', return_value=supported_executor()):
        with CaptureQueriesContext(connection) as queries:
            assert serializer.get_attempts_count(job) == 7
            assert serializer.get_source_binding_status(job) == 'ready'
            assert serializer.get_executor_status(job) == 'ready'
            assert serializer.get_execution_supported(job) is True

    assert len(queries) == 0


@pytest.mark.django_db
def test_job_serializer_batch_map_missing_key_does_not_query_and_legacy_fallback_is_cached(execution_job):
    _user, _project, job = execution_job
    batch_serializer = ExecutionJobSerializer(instance=job, context={'source_binding_statuses': {}})
    with CaptureQueriesContext(connection) as queries:
        assert batch_serializer.get_source_binding_status(job) == 'missing'
        job.source_id = 'malformed-source'
        assert batch_serializer.get_source_binding_status(job) == 'invalid'
        job.source_type = 'manual'
        assert batch_serializer.get_source_binding_status(job) == 'not_required'
    assert len(queries) == 0

    fallback_serializer = ExecutionJobSerializer(instance=job)
    job.source_type = 'api'
    job.source_id = 'api:suite:42'
    with patch('apps.executions.adapters.get_executor_adapter', return_value=supported_executor()):
        with CaptureQueriesContext(connection) as queries:
            assert fallback_serializer.get_source_binding_status(job) == 'missing'
            assert fallback_serializer.get_executor_status(job) == 'missing_binding'
            assert fallback_serializer.get_execution_supported(job) is False
    assert len(queries) == 1


@pytest.mark.django_db
def test_job_serializer_attempt_count_falls_back_without_annotation(execution_job):
    _user, _project, job = execution_job
    ExecutionAttempt.objects.create(job=job, attempt_number=1)
    serializer = ExecutionJobSerializer(instance=job)
    with CaptureQueriesContext(connection) as queries:
        assert serializer.get_attempts_count(job) == 1
    assert len(queries) == 1


@pytest.mark.django_db
def test_notification_event_serialization_redacts_errors_and_is_entirely_read_only(execution_job):
    _user, project, _job = execution_job
    subscription = ExecutionNotificationSubscription(
        pk=100,
        project=project,
        channel_id=200,
        event_type='gate_failed',
    )
    event = ExecutionNotificationEvent(
        pk=300,
        project=project,
        subscription=subscription,
        event_key='legacy-event',
        event_type='gate_failed',
        status='failed',
        payload={'api_token': 'payload-secret'},
        error_message='Request failed: password=old-secret; Authorization: Bearer legacy-token',
    )

    serializer = ExecutionNotificationEventSerializer(instance=event)
    rendered = serializer.data
    assert '[REDACTED]' in rendered['error_message']
    assert 'old-secret' not in rendered['error_message']
    assert 'legacy-token' not in rendered['error_message']
    assert rendered['payload']['api_token'] == '[REDACTED]'
    assert all(field.read_only for field in serializer.fields.values())
    assert 'delivery_claim_id' in serializer.fields
    assert 'delivery_attempts' in serializer.fields
    assert 'next_attempt_at' in serializer.fields
