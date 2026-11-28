from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('api_testing', '0005_api_test_cases')]

    operations = [
        migrations.AddField(
            model_name='apicaseexecution',
            name='heartbeat_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='apicaseexecution',
            index=models.Index(fields=['status', 'heartbeat_at'], name='api_case_exec_state_hb_idx'),
        ),
    ]
