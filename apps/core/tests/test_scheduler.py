import pytest
from django.utils import timezone
from datetime import timedelta

from apps.api_testing.models import ScheduledTask
from apps.core.scheduler import ScheduleClaimLost, claim_due_task, load_claimed_task
from apps.users.models import User


@pytest.mark.django_db
def test_scheduler_claim_is_single_owner_and_expired_claim_can_be_reclaimed():
    owner = User.objects.create_user(username='scheduler-owner', password='pass')
    now = timezone.now()
    task = ScheduledTask.objects.create(
        name='due-task',
        task_type='API_REQUEST',
        trigger_type='INTERVAL',
        interval_seconds=60,
        status='ACTIVE',
        next_run_time=now - timedelta(seconds=1),
        created_by=owner,
    )

    first = claim_due_task(ScheduledTask, task.pk, due_field='next_run_time', now=now)
    assert first is not None
    assert claim_due_task(ScheduledTask, task.pk, due_field='next_run_time', now=now) is None
    assert load_claimed_task(ScheduledTask, task.pk, first.token, require_active=True).pk == task.pk

    expired_at = now + timedelta(seconds=301)
    second = claim_due_task(
        ScheduledTask,
        task.pk,
        due_field='next_run_time',
        now=expired_at,
    )
    assert second is not None
    assert second.token != first.token
    with pytest.raises(ScheduleClaimLost):
        load_claimed_task(ScheduledTask, task.pk, first.token, require_active=True)
