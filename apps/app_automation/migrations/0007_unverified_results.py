from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('app_automation', '0006_app_execution_stop_intent')]

    operations = [
        migrations.AlterField(
            model_name='apptestexecution',
            name='result',
            field=models.CharField(
                blank=True, null=True, default=None, max_length=20,
                choices=[('passed', '通过'), ('failed', '失败'), ('skipped', '跳过'), ('unverified', '未验证')],
                verbose_name='测试结果',
            ),
        ),
        migrations.AlterField(
            model_name='apptestsuite',
            name='execution_result',
            field=models.CharField(
                blank=True, null=True, default=None, max_length=20,
                choices=[('passed', '通过'), ('failed', '失败'), ('skipped', '跳过'), ('unverified', '未验证')],
                verbose_name='测试结果',
            ),
        ),
    ]
