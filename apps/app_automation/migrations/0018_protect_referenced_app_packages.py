# Protect case and schedule references from accidental package deletion.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_automation", "0017_appium_session_device_set_null"),
    ]

    operations = [
        migrations.AlterField(
            model_name="apptestcase",
            name="app_package",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="test_cases",
                to="app_automation.apppackage",
                verbose_name="应用包名",
            ),
        ),
        migrations.AlterField(
            model_name="appscheduledtask",
            name="app_package",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="scheduled_tasks",
                to="app_automation.apppackage",
                verbose_name="应用包名",
            ),
        ),
    ]
