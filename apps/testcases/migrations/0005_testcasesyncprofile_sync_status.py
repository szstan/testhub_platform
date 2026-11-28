from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('testcases', '0004_testcasesyncprofile_testcasesourcebinding_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcasesyncprofile',
            name='sync_status',
            field=models.CharField(
                choices=[
                    ('idle', 'Idle'),
                    ('queued', 'Queued'),
                    ('running', 'Running'),
                    ('completed', 'Completed'),
                    ('failed', 'Failed'),
                ],
                default='idle',
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='testcasesyncprofile',
            name='sync_task_id',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='testcasesyncprofile',
            name='sync_result',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='testcasesyncprofile',
            name='sync_error',
            field=models.TextField(blank=True, default=''),
        ),
    ]
