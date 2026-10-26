from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('app_automation', '0003_appdevice_capabilities_appdevice_health_status_and_more')]

    operations = [
        migrations.AddField(
            model_name='appscheduledtask',
            name='scheduler_lease_token',
            field=models.CharField(blank=True, default='', max_length=64, verbose_name='调度租约令牌'),
        ),
        migrations.AddField(
            model_name='appscheduledtask',
            name='scheduler_lease_expires_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='调度租约过期时间'),
        ),
        migrations.AddIndex(
            model_name='appscheduledtask',
            index=models.Index(fields=['scheduler_lease_expires_at'], name='app_sched_lease_exp_idx'),
        ),
    ]
