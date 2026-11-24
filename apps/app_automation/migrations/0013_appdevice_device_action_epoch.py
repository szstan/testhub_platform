from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_automation", "0012_appdevice_lease_max_seconds"),
    ]

    operations = [
        migrations.AddField(
            model_name="appdevice",
            name="device_action_epoch",
            field=models.PositiveBigIntegerField(
                default=0,
                editable=False,
                verbose_name="设备动作世代",
            ),
        ),
    ]
