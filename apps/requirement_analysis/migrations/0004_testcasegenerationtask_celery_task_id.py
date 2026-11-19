# Generated for the durable Celery claim fence.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0003_testcasegenerationtask_adopted_case_ids'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='celery_task_id',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='Celery任务ID'),
        ),
    ]
