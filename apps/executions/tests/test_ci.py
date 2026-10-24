import hashlib
import hmac
import time

import pytest
from rest_framework.test import APIClient

from apps.executions.ci import quality_gate_result
from apps.executions.models import ExecutionBuild, ExecutionJob, ExecutionProfile, WebhookEvent
from apps.projects.models import Project
from apps.users.models import User


def _signature(secret, timestamp, raw):
    return hmac.new(secret.encode(), f'{timestamp}.'.encode() + raw, hashlib.sha256).hexdigest()


@pytest.mark.django_db
def test_signed_ci_webhook_is_idempotent_and_creates_build_job(monkeypatch):
    owner = User.objects.create_user(username='ci-owner', password='pass')
    project = Project.objects.create(name='CI Project', owner=owner)
    profile = ExecutionProfile.objects.create(
        project=project,
        name='generic-ci',
        provider='generic',
        source_type='api',
        webhook_secret_ref='TESTHUB_TEST_CI_SECRET',
        webhook_enabled=True,
        created_by=owner,
    )
    secret = 'ci-test-secret'
    monkeypatch.setenv('TESTHUB_TEST_CI_SECRET', secret)
    raw = b'{"build_number":"build-9","commit_sha":"abc123","branch":"main"}'
    timestamp = str(int(time.time()))
    headers = {
        'HTTP_X_TESTHUB_PROFILE': str(profile.id),
        'HTTP_X_TESTHUB_EVENT': 'event-9',
        'HTTP_X_TESTHUB_TIMESTAMP': timestamp,
        'HTTP_X_TESTHUB_SIGNATURE': _signature(secret, timestamp, raw),
    }
    client = APIClient()
    first = client.generic('POST', '/api/executions/webhooks/generic/', raw, content_type='application/json', **headers)
    assert first.status_code == 202
    assert ExecutionBuild.objects.filter(project=project, build_number='build-9').exists()
    assert ExecutionJob.objects.filter(project=project, source_id='event-9').exists()
    assert WebhookEvent.objects.count() == 1

    replay = client.generic('POST', '/api/executions/webhooks/generic/', raw, content_type='application/json', **headers)
    assert replay.status_code == 200
    assert WebhookEvent.objects.count() == 1


@pytest.mark.django_db
def test_ci_webhook_rejects_invalid_signature(monkeypatch):
    owner = User.objects.create_user(username='ci-invalid-owner', password='pass')
    project = Project.objects.create(name='Invalid CI Project', owner=owner)
    profile = ExecutionProfile.objects.create(
        project=project,
        name='invalid-ci',
        provider='generic',
        webhook_secret_ref='TESTHUB_INVALID_SECRET',
        webhook_enabled=True,
        created_by=owner,
    )
    monkeypatch.setenv('TESTHUB_INVALID_SECRET', 'expected-secret')
    raw = b'{"build_number":"bad"}'
    response = APIClient().generic(
        'POST',
        '/api/executions/webhooks/generic/',
        raw,
        content_type='application/json',
        HTTP_X_TESTHUB_PROFILE=str(profile.id),
        HTTP_X_TESTHUB_EVENT='bad-event',
        HTTP_X_TESTHUB_TIMESTAMP=str(int(time.time())),
        HTTP_X_TESTHUB_SIGNATURE='00' * 32,
    )
    assert response.status_code == 401
    assert WebhookEvent.objects.count() == 0


def test_quality_gate_result_is_deterministic():
    assert quality_gate_result(None, {'pass_rate': 99}) == {'passed': True, 'failures': []}
