from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.executions.models import ExecutionNotificationEvent, ExecutionNotificationSubscription
from apps.executions.notifications import emit_project_notification, notify_integration_failed, notify_quarantine_expiring
from apps.monitor.models import NotificationChannel
from apps.projects.models import Project
from apps.users.models import User


@pytest.mark.django_db
def test_execution_notification_is_idempotent_and_delivery_failure_is_recorded():
    owner = User.objects.create_user(username='notify-owner', password='pass')
    project = Project.objects.create(name='Notify Project', owner=owner)
    channel = NotificationChannel.objects.create(
        name='notify-channel', type='EMAIL', config={}, created_by=owner,
    )
    subscription = ExecutionNotificationSubscription.objects.create(
        project=project,
        channel=channel,
        event_type='gate_failed',
        created_by=owner,
    )

    with patch('apps.executions.notifications.send_via_channel', return_value=(False, 'provider down')) as send:
        first = emit_project_notification(project, 'gate_failed', 'gate:1', {'token': 'secret'})
        second = emit_project_notification(project, 'gate_failed', 'gate:1', {'token': 'secret'})

    assert len(first) == 1
    assert len(second) == 1
    event = ExecutionNotificationEvent.objects.get(subscription=subscription, event_key='gate:1')
    assert event.status == 'failed'
    assert event.error_message == 'provider down'
    assert 'secret' not in str(event.payload)
    assert send.call_count == 2

    integration_subscription = ExecutionNotificationSubscription.objects.create(
        project=project,
        channel=channel,
        event_type='integration_failed',
        created_by=owner,
    )
    quarantine_subscription = ExecutionNotificationSubscription.objects.create(
        project=project,
        channel=channel,
        event_type='quarantine_expiring',
        created_by=owner,
    )
    with patch('apps.executions.notifications.send_via_channel', return_value=(True, 'ok')):
        notify_integration_failed(project, 'integration:1', {'source': 'webhook'})
        notify_quarantine_expiring(project, 'quarantine:1', {'profile_id': 1})
    assert ExecutionNotificationEvent.objects.filter(subscription=integration_subscription, status='sent').exists()
    assert ExecutionNotificationEvent.objects.filter(subscription=quarantine_subscription, status='sent').exists()


@pytest.mark.django_db
def test_notification_subscription_is_project_scoped():
    owner = User.objects.create_user(username='notify-api-owner', password='pass')
    outsider = User.objects.create_user(username='notify-api-outsider', password='pass')
    project = Project.objects.create(name='Notify API Project', owner=owner)
    channel = NotificationChannel.objects.create(
        name='notify-api-channel', type='EMAIL', config={}, created_by=owner,
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    created = client.post(
        '/api/executions/notification-subscriptions/',
        {'project': project.pk, 'channel': channel.pk, 'event_type': 'gate_failed'},
        format='json',
    )
    assert created.status_code == 201, created.data

    client.force_authenticate(user=outsider)
    assert client.get('/api/executions/notification-subscriptions/').data['count'] == 0
    assert client.get(f"/api/executions/notification-subscriptions/{created.data['id']}/").status_code == 404
