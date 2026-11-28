from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def bind_legacy_channel_owners(apps, schema_editor):
    from django.db.models import F
    Task = apps.get_model('perf_testing', 'PerfScheduledTask')
    Task.objects.using(schema_editor.connection.alias).filter(
        notify_channels_set_by__isnull=True,
    ).update(notify_channels_set_by_id=F('created_by_id'))


class Migration(migrations.Migration):
    dependencies = [
        ('perf_testing', '0013_perfexecution_admission_lock'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='perfscheduledtask',
            name='notify_channels_set_by',
            field=models.ForeignKey(
                blank=True,
                db_comment='最后配置通知渠道的用户(发送时复核所有权)',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='configured_perf_notification_tasks',
                to=settings.AUTH_USER_MODEL,
                verbose_name='通知渠道配置人',
            ),
        ),
        migrations.RunPython(bind_legacy_channel_owners, migrations.RunPython.noop),
    ]
