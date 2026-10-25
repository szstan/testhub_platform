import pytest
from django.urls import include, path
from rest_framework.test import APIClient

from apps.monitor.models import AlertEvent, MonitorCheckLog, MonitorTarget
from apps.users.models import User


urlpatterns = [path('api/monitor/', include('apps.monitor.urls'))]


@pytest.fixture(autouse=True)
def isolated_urls(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def monitor_boundary_data(db):
    owner = User.objects.create_user(username='monitor-write-owner', password='pass')
    outsider = User.objects.create_user(username='monitor-write-outsider', password='pass')
    target = MonitorTarget.objects.create(
        name='monitor-write-owner-target',
        type='HTTP',
        url='https://example.com/health',
        created_by=owner,
    )
    other_target = MonitorTarget.objects.create(
        name='monitor-write-outsider-target',
        type='HTTP',
        url='https://example.org/health',
        created_by=outsider,
    )
    return locals()


def monitor_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_check_log_mutations_require_target_ownership(monitor_boundary_data):
    data = monitor_boundary_data
    client = monitor_client(data['owner'])

    created = client.post(
        '/api/monitor/checks/',
        {
            'target': data['target'].id,
            'status': 'UP',
            'latency_ms': 12,
            'http_status': 200,
            'error_message': '',
            'detail': {},
            'triggered_alert': False,
        },
        format='json',
    )
    assert created.status_code == 201, created.data
    log_id = created.data['id']

    valid_update = client.patch(
        f'/api/monitor/checks/{log_id}/',
        {'status': 'DOWN'},
        format='json',
    )
    assert valid_update.status_code == 200, valid_update.data

    denied_create = client.post(
        '/api/monitor/checks/',
        {
            'target': data['other_target'].id,
            'status': 'UP',
            'detail': {},
            'triggered_alert': False,
        },
        format='json',
    )
    assert denied_create.status_code == 403, denied_create.data

    denied_update = client.patch(
        f'/api/monitor/checks/{log_id}/',
        {'target': data['other_target'].id},
        format='json',
    )
    assert denied_update.status_code == 403, denied_update.data

    assert MonitorCheckLog.objects.filter(pk=log_id, target=data['target']).exists()


@pytest.mark.django_db
def test_alert_mutations_require_target_ownership(monitor_boundary_data):
    data = monitor_boundary_data
    client = monitor_client(data['owner'])

    created = client.post(
        '/api/monitor/alerts/',
        {
            'target': data['target'].id,
            'level': 'CRITICAL',
            'message': 'same project alert',
            'status': 'FIRING',
            'send_detail': [],
            'notify_count': 0,
        },
        format='json',
    )
    assert created.status_code == 201, created.data
    alert_id = created.data['id']

    valid_update = client.patch(
        f'/api/monitor/alerts/{alert_id}/',
        {'message': 'updated alert'},
        format='json',
    )
    assert valid_update.status_code == 200, valid_update.data

    denied_create = client.post(
        '/api/monitor/alerts/',
        {
            'target': data['other_target'].id,
            'level': 'WARNING',
            'message': 'cross owner alert',
            'status': 'FIRING',
            'send_detail': [],
            'notify_count': 0,
        },
        format='json',
    )
    assert denied_create.status_code == 403, denied_create.data

    denied_update = client.patch(
        f'/api/monitor/alerts/{alert_id}/',
        {'target': data['other_target'].id},
        format='json',
    )
    assert denied_update.status_code == 403, denied_update.data

    assert AlertEvent.objects.filter(pk=alert_id, target=data['target']).exists()
