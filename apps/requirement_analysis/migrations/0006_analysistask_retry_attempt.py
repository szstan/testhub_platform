from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('requirement_analysis', '0005_testcasegenerationtask_worker_claim_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysistask',
            name='retry_attempt',
            field=models.PositiveIntegerField(default=0, verbose_name='重试次数'),
        ),
    ]
