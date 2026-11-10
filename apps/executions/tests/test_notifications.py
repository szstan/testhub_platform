from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.executions.models import ExecutionNotificationEvent, ExecutionNotificationSubscription
from apps.executions.notification_tasks import (
    DELIVERY_LEASE_SECONDS,
    RETRY_MAX_SECONDS,
    deliver_execution_notification,
)
from apps.executions.notifications import (
    MAX_DELIVERY_ATTEMPTS,
    NOTIFICATION_TASK,
    dispatch_pending_notifications,
    emit_project_notification,
    notify_integration_failed,
    notify_quarantine_expiring,
)
from apps.monitor.models import NotificationChannel
from apps.projects.models import Project
from apps.users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def subscription():
    owner = User.objects.create_user(username='notify-owner', password='pass')
    project = Project.objects.create(name='Notify Project', owner=owner)
    channel = NotificationChannel.objects.create(
        name='notify-channel', type='EMAIL', config={}, created_by=owner,
    )
    return ExecutionNotificationSubscription.objects.create(
        project=project, channel=channel, event_type='gate_failed', created_by=owner,
    )


def _event(subscription, **overrides):
    values = {
        'project': subscription.project,
        'subscription': subscription,
        'event_type': subscription.event_type,
        'event_key': 'gate:1',
        'payload': {'message': 'gate failed'},
    }
    values.update(overrides)
    return ExecutionNotificationEvent.objects.create(**values)


def test_emit_is_an_immutable_deduplicated_intent_enqueued_only_on_commit(
    subscription, django_capture_on_commit_callbacks,
):
    project = subscription.project
    with (
        patch('apps.executions.notifications.current_app.send_task') as enqueue,
        patch('apps.executions.notification_tasks.send_via_channel') as send,
        django_capture_on_commit_callbacks(execute=True) as callbacks,
    ):
        with transaction.atomic():
            first = emit_project_notification(project, 'gate_failed', 'gate:1', {'token': 'secret'})
            second = emit_project_notification(project, 'gate_failed', 'gate:1', {'message': 'changed'})
            assert first[0].pk == second[0].pk
            assert first[0].status == 'pending'
            enqueue.assert_not_called()
            send.assert_not_called()
    assert len(callbacks) == 1
    enqueue.assert_called_once_with(NOTIFICATION_TASK, args=[first[0].pk], retry=False)
    send.assert_not_called()
    event = ExecutionNotificationEvent.objects.get(pk=first[0].pk)
    assert event.payload == {'token': '[REDACTED]'}
    assert event.delivery_attempts == 0


def test_rollback_discards_intent_and_enqueue(subscription, django_capture_on_commit_callbacks):
    with (
        patch('apps.executions.notifications.current_app.send_task') as enqueue,
        django_capture_on_commit_callbacks(execute=True) as callbacks,
    ):
        with pytest.raises(ValueError), transaction.atomic():
            emit_project_notification(subscription.project, 'gate_failed', 'rollback', {})
            raise ValueError('rollback business event')
    assert callbacks == []
    enqueue.assert_not_called()
    assert not ExecutionNotificationEvent.objects.exists()


def test_notification_helpers_keep_their_event_types(subscription, django_capture_on_commit_callbacks):
    project, channel = subscription.project, subscription.channel
    for event_type in ('integration_failed', 'quarantine_expiring'):
        ExecutionNotificationSubscription.objects.create(
            project=project, channel=channel, event_type=event_type,
        )
    with (
        patch('apps.executions.notifications.current_app.send_task') as enqueue,
        django_capture_on_commit_callbacks(execute=True),
    ):
        integration = notify_integration_failed(project, 'integration:1', {'source': 'webhook'})
        quarantine = notify_quarantine_expiring(project, 'quarantine:1', {'profile_id': 1})
    assert integration[0].event_type == 'integration_failed'
    assert quarantine[0].event_type == 'quarantine_expiring'
    assert enqueue.call_count == 2


def test_intent_keys_preserve_identity_without_retaining_credentials(subscription):
    project = subscription.project
    a = emit_project_notification(project, 'gate_failed', 'x' * 256 + 'a', {})[0]
    b = emit_project_notification(project, 'gate_failed', 'x' * 256 + 'b', {})[0]
    c = emit_project_notification(project, 'gate_failed', 'token=key-secret', {})[0]
    assert a.pk != b.pk
    assert all(len(row.event_key) <= 255 for row in (a, b, c))
    assert 'key-secret' not in c.event_key


@pytest.mark.django_db(transaction=True)
def test_delivery_runs_without_database_transaction_and_live_claim_suppresses_duplicates(subscription):
    event = _event(subscription, payload={'token': 'legacy-secret'})

    def provider(channel, *, message, subject):
        assert not connection.in_atomic_block
        assert channel.pk == subscription.channel_id
        assert subject == 'TestHub gate_failed'
        assert 'legacy-secret' not in message
        event.refresh_from_db()
        assert event.status == 'dispatching'
        assert event.delivery_claim_id
        assert event.delivery_claim_expires_at > timezone.now()
        assert deliver_execution_notification(event.pk)['status'] == 'busy'
        return True, 'ok'

    with patch('apps.executions.notification_tasks.send_via_channel', side_effect=provider) as send:
        assert deliver_execution_notification(event.pk)['status'] == 'sent'
        assert deliver_execution_notification(event.pk)['status'] == 'sent'
    send.assert_called_once()
    event.refresh_from_db()
    assert event.sent_at is not None
    assert event.delivery_attempts == 1
    assert event.delivery_claim_id == ''
    assert event.delivery_claim_expires_at is None
    assert event.next_attempt_at is None
    assert event.payload == {'token': '[REDACTED]'}
    with patch('apps.executions.notifications.current_app.send_task') as enqueue:
        emit_project_notification(subscription.project, 'gate_failed', 'gate:1', {'message': 'new'})
    enqueue.assert_not_called()
    event.refresh_from_db()
    assert event.status == 'sent'


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('raise_error', [False, True])
def test_failed_delivery_is_redacted_and_retried_only_when_due(subscription, raise_error):
    event = _event(subscription)
    detail = 'password=smtp-secret https://user:url-secret@example.test/path?token=query-secret Bearer bearer-secret'
    provider_options = {'side_effect': RuntimeError(detail)} if raise_error else {'return_value': (False, detail)}
    with patch('apps.executions.notification_tasks.send_via_channel', **provider_options) as send:
        assert deliver_execution_notification(event.pk)['status'] == 'failed'
        event.refresh_from_db()
        assert event.status == 'failed'
        assert event.delivery_attempts == 1
        assert event.next_attempt_at > timezone.now()
        assert all(value not in event.error_message for value in (
            'smtp-secret', 'url-secret', 'query-secret', 'bearer-secret',
        ))
        assert deliver_execution_notification(event.pk)['status'] == 'deferred'
        with patch('apps.executions.notifications.current_app.send_task') as enqueue:
            emit_project_notification(subscription.project, 'gate_failed', 'gate:1', {})
            assert dispatch_pending_notifications() == 0
        enqueue.assert_not_called()
    send.assert_called_once()
    due_at = event.next_attempt_at
    with (
        patch('apps.executions.notification_tasks.timezone.now', return_value=due_at),
        patch('apps.executions.notifications.current_app.send_task') as enqueue,
        patch('apps.executions.notification_tasks.send_via_channel', return_value=(True, 'ok')) as send,
    ):
        assert dispatch_pending_notifications() == 1
        enqueue.assert_called_once_with(NOTIFICATION_TASK, args=[event.pk], retry=False)
        send.assert_not_called()
        assert deliver_execution_notification(event.pk)['status'] == 'sent'
    event.refresh_from_db()
    assert event.delivery_attempts == 2
    assert event.error_message == ''
    assert event.next_attempt_at is None


@pytest.mark.django_db(transaction=True)
def test_retry_budget_is_bounded_and_terminal_failure_is_not_redispatched(subscription):
    event = _event(subscription)
    now = timezone.now()
    with patch('apps.executions.notification_tasks.send_via_channel', return_value=(False, 'provider down')) as send:
        for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
            with patch('apps.executions.notification_tasks.timezone.now', return_value=now):
                assert deliver_execution_notification(event.pk)['status'] == 'failed'
            event.refresh_from_db()
            assert event.delivery_attempts == attempt
            if attempt < MAX_DELIVERY_ATTEMPTS:
                assert 0 < (event.next_attempt_at - now).total_seconds() <= RETRY_MAX_SECONDS
                now = event.next_attempt_at
        assert event.next_attempt_at is None
        assert deliver_execution_notification(event.pk)['status'] == 'failed'
    assert send.call_count == MAX_DELIVERY_ATTEMPTS
    with patch('apps.executions.notifications.current_app.send_task') as enqueue:
        assert dispatch_pending_notifications() == 0
    enqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_expired_worker_cannot_overwrite_new_delivery(subscription):
    event = _event(subscription)
    calls = []

    def provider(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            # Simulate worker stall, lease expiry and a replacement worker.
            ExecutionNotificationEvent.objects.filter(pk=event.pk).update(
                delivery_claim_expires_at=timezone.now() - timedelta(seconds=1),
            )
            assert deliver_execution_notification(event.pk)['status'] == 'sent'
            return False, 'old worker failed'
        return True, 'ok'

    with patch('apps.executions.notification_tasks.send_via_channel', side_effect=provider):
        assert deliver_execution_notification(event.pk)['status'] == 'stale'
    event.refresh_from_db()
    assert len(calls) == 2
    assert event.status == 'sent'
    assert event.delivery_attempts == 2
    assert event.error_message == ''
    assert event.sent_at is not None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('attempts', [1, MAX_DELIVERY_ATTEMPTS])
def test_abandoned_claim_is_recovered_or_exhausted(subscription, attempts):
    event = _event(
        subscription, status='dispatching', delivery_claim_id='dead-worker',
        delivery_claim_expires_at=timezone.now() - timedelta(seconds=DELIVERY_LEASE_SECONDS),
        delivery_attempts=attempts,
    )
    with (
        patch('apps.executions.notifications.current_app.send_task') as enqueue,
        patch('apps.executions.notification_tasks.send_via_channel', return_value=(True, 'ok')) as send,
    ):
        assert dispatch_pending_notifications() == 1
        enqueue.assert_called_once()
        send.assert_not_called()
        result = deliver_execution_notification(event.pk)
        assert result['status'] == ('sent' if attempts < MAX_DELIVERY_ATTEMPTS else 'failed')
    event.refresh_from_db()
    assert event.delivery_claim_id == ''
    assert event.delivery_claim_expires_at is None
    assert send.call_count == (1 if attempts < MAX_DELIVERY_ATTEMPTS else 0)
    assert event.next_attempt_at is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('change', ['subscription_disabled', 'channel_disabled', 'project', 'event_type'])
def test_delivery_refuses_disabled_or_changed_subscription_scope(subscription, change):
    event = _event(subscription)
    if change == 'subscription_disabled':
        subscription.enabled = False
        subscription.save(update_fields=['enabled'])
    elif change == 'channel_disabled':
        channel = subscription.channel
        channel.enabled = False
        channel.save(update_fields=['enabled'])
    elif change == 'project':
        subscription.project = Project.objects.create(name='Other Project', owner=subscription.created_by)
        subscription.save(update_fields=['project'])
    else:
        subscription.event_type = 'integration_failed'
        subscription.save(update_fields=['event_type'])
    with patch('apps.executions.notification_tasks.send_via_channel') as send:
        assert deliver_execution_notification(event.pk)['status'] == 'failed'
        assert deliver_execution_notification(event.pk)['status'] == 'failed'
    send.assert_not_called()
    event.refresh_from_db()
    assert event.next_attempt_at is None
    assert event.delivery_claim_id == ''
    assert event.sent_at is None


@pytest.mark.django_db(transaction=True)
def test_scope_is_rechecked_after_claim_before_provider_io(subscription):
    from apps.executions.notification_tasks import _claim_delivery

    event = _event(subscription)

    def claim_then_disable(event_id):
        result = _claim_delivery(event_id)
        ExecutionNotificationSubscription.objects.filter(pk=subscription.pk).update(enabled=False)
        return result

    with (
        patch('apps.executions.notification_tasks._claim_delivery', side_effect=claim_then_disable),
        patch('apps.executions.notification_tasks.send_via_channel') as send,
    ):
        assert deliver_execution_notification(event.pk)['status'] == 'failed'
    send.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_broker_outage_keeps_committed_intent_recoverable(subscription):
    with (
        patch('apps.executions.notifications.current_app.send_task', side_effect=RuntimeError('token=broker-secret')),
        patch('apps.executions.notification_tasks.send_via_channel') as send,
    ):
        with transaction.atomic():
            event = emit_project_notification(subscription.project, 'gate_failed', 'broker:1', {})[0]
    send.assert_not_called()
    event.refresh_from_db()
    assert event.status == 'pending'
    assert event.delivery_attempts == 0
    assert 'broker-secret' not in event.error_message
    with patch('apps.executions.notifications.current_app.send_task') as enqueue:
        assert dispatch_pending_notifications(limit=1) == 1
    enqueue.assert_called_once_with(NOTIFICATION_TASK, args=[event.pk], retry=False)


@pytest.mark.django_db(transaction=True)
def test_scheduler_selects_only_due_or_abandoned_rows_and_honors_limit(subscription):
    now = timezone.now()
    rows = [
        _event(subscription, event_key='pending'),
        _event(subscription, event_key='future', next_attempt_at=now + timedelta(hours=1)),
        _event(subscription, event_key='retry', status='failed', next_attempt_at=now - timedelta(seconds=1), delivery_attempts=1),
        _event(subscription, event_key='terminal', status='failed', delivery_attempts=MAX_DELIVERY_ATTEMPTS),
        _event(subscription, event_key='sent', status='sent'),
        _event(subscription, event_key='live', status='dispatching', delivery_claim_expires_at=now + timedelta(minutes=2)),
        _event(subscription, event_key='abandoned', status='dispatching'),
    ]
    with patch('apps.executions.notifications.current_app.send_task') as enqueue:
        assert dispatch_pending_notifications(limit=2) == 2
        assert [call.kwargs['args'][0] for call in enqueue.call_args_list] == [rows[0].pk, rows[2].pk]
        enqueue.reset_mock()
        assert dispatch_pending_notifications(limit=0) == 0
        enqueue.assert_not_called()
        assert dispatch_pending_notifications(limit=100) == 3


@pytest.mark.django_db(transaction=True)
def test_delivery_and_dispatch_refuse_an_outer_business_transaction(subscription):
    event = _event(subscription)
    with transaction.atomic():
        with pytest.raises(RuntimeError, match='outside a database transaction'):
            deliver_execution_notification(event.pk)
        with pytest.raises(RuntimeError, match='outside a database transaction'):
            dispatch_pending_notifications()
    with patch('apps.executions.notification_tasks.send_via_channel') as send:
        assert deliver_execution_notification(9999999)['status'] == 'missing'
    send.assert_not_called()


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
