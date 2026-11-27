from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('app_automation', '0005_device_lease_generation')]

    operations = [
        migrations.AddField(
            model_name='apptestexecution',
            name='stop_requested',
            field=models.BooleanField(default=False, verbose_name='是否请求停止'),
        ),
        migrations.AddField(
            model_name='apptestexecution',
            name='release_device_lease_on_stop',
            field=models.BooleanField(default=False, verbose_name='停止时释放执行租约'),
        ),
    ]
