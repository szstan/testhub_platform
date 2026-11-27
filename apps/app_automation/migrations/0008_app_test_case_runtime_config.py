from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('app_automation', '0007_unverified_results')]

    operations = [
        migrations.AddField(
            model_name='apptestcase',
            name='runtime_config',
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='重试参数及 OCR 配置；GPU 选项仅作用于执行服务端，不代表移动设备兼容性',
                verbose_name='场景运行时配置',
            ),
        ),
    ]
