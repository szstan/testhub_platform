from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('executions', '0019_execution_delivery_leases')]

    operations = [
        migrations.AddField(
            model_name='testruncasehistory', name='defects',
            field=models.JSONField(blank=True, null=True, verbose_name='关联缺陷'),
        ),
        migrations.AddField(
            model_name='testruncasehistory', name='elapsed_time',
            field=models.DurationField(blank=True, null=True, verbose_name='执行耗时'),
        ),
    ]
