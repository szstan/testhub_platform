from datetime import timedelta

from django.db import migrations, models
from django.utils import timezone


def initialize_legacy_recovery_state(apps, schema_editor):
    AnalysisTask = apps.get_model('requirement_analysis', 'AnalysisTask')
    now = timezone.now()
    AnalysisTask.objects.exclude(status='pending').update(
        dispatch_state='published',
        dispatch_acknowledged_at=now,
    )
    # Legacy pending deliveries have no durable publisher receipt. Re-publish
    # their stable claim once; the worker claim CAS makes duplicate deliveries harmless.
    AnalysisTask.objects.filter(status='pending').update(
        dispatch_state='unpublished',
        dispatch_acknowledged_at=None,
    )
    AnalysisTask.objects.filter(status='running').update(
        worker_heartbeat_at=now,
        worker_lease_expires_at=now + timedelta(seconds=1800),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0006_analysistask_retry_attempt'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysistask',
            name='worker_invocation_nonce',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='analysistask',
            name='worker_heartbeat_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='analysistask',
            name='worker_lease_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='analysistask',
            name='dispatch_state',
            field=models.CharField(
                choices=[('unpublished', '未发布'), ('publishing', '发布中'), ('published', '已确认')],
                default='unpublished',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='analysistask',
            name='dispatch_nonce',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='analysistask',
            name='dispatch_lease_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='analysistask',
            name='dispatch_acknowledged_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(initialize_legacy_recovery_state, migrations.RunPython.noop),
    ]
