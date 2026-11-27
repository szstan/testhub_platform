# Generated for Appium device history retention.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_automation", "0016_app_flow_failure"),
    ]

    operations = [
        migrations.AlterField(
            model_name="appiumsession",
            name="device",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="appium_sessions",
                to="app_automation.appdevice",
            ),
        ),
    ]
