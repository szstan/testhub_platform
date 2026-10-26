from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('perf_testing', '0011_alter_perfenvironment_verify_ssl')]

    operations = [
        migrations.AddField(
            model_name='perfscheduledtask',
            name='scheduler_lease_token',
            field=models.CharField(blank=True, db_comment='调度租约令牌', default='', max_length=64, verbose_name='调度租约令牌'),
        ),
        migrations.AddField(
            model_name='perfscheduledtask',
            name='scheduler_lease_expires_at',
            field=models.DateTimeField(blank=True, db_comment='调度租约过期时间', null=True, verbose_name='调度租约过期时间'),
        ),
        migrations.AddIndex(
            model_name='perfscheduledtask',
            index=models.Index(fields=['scheduler_lease_expires_at'], name='perf_sched_lease_exp_idx'),
        ),
    ]
