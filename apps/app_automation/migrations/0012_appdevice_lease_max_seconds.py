from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_automation", "0011_device_action_pending_on_device"),
    ]

    operations = [
        migrations.AddField(
            model_name="appdevice",
            name="lease_max_seconds",
            field=models.PositiveIntegerField(
                blank=True,
                default=None,
                null=True,
                verbose_name="租约续期上限(秒)",
            ),
        ),
    ]
