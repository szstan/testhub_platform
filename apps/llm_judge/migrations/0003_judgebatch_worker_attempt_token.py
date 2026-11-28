from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('llm_judge', '0002_judgebatchcaseresult_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='judgebatch',
            name='worker_attempt_token',
            field=models.CharField(
                blank=True,
                default='',
                max_length=32,
                verbose_name='工作进程尝试令牌',
            ),
        ),
    ]
