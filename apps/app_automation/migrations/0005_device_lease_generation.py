from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('app_automation', '0004_scheduled_task_lease')]

    operations = [
        migrations.AddField(
            model_name='appdevice',
            name='lease_generation',
            field=models.PositiveBigIntegerField(default=0, verbose_name='租约世代'),
        ),
        migrations.AddField(
            model_name='apptestexecution',
            name='device_lease_generation',
            field=models.PositiveBigIntegerField(blank=True, null=True, verbose_name='设备租约世代'),
        ),
    ]
