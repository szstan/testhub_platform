from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('ui_automation', '0003_scheduled_task_lease')]

    operations = [
        migrations.AddField(
            model_name='testexecution',
            name='failure_summary',
            field=models.JSONField(blank=True, default=dict, null=True, editable=False),
        ),
        migrations.AddField(
            model_name='testcaseexecution',
            name='failure_summary',
            field=models.JSONField(blank=True, default=dict, null=True, editable=False),
        ),
    ]
