from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0004_testcasegenerationtask_celery_task_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='testcasegenerationtask',
            name='worker_claim_id',
            field=models.CharField(blank=True, default='', max_length=64, verbose_name='Worker执行认领ID'),
        ),
    ]
