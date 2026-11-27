from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app_automation", "0013_appdevice_device_action_epoch"),
    ]

    operations = [
        migrations.AddField(
            model_name="appproject",
            name="appium_enabled",
            field=models.BooleanField(default=False, verbose_name="启用 Appium 引擎"),
        ),
        migrations.AddField(
            model_name="apptestcase",
            name="engine",
            field=models.CharField(
                choices=[("legacy", "Legacy"), ("appium", "Appium")],
                default="legacy",
                max_length=20,
                verbose_name="执行引擎",
            ),
        ),
        migrations.AddField(
            model_name="apptestexecution",
            name="engine",
            field=models.CharField(
                choices=[("legacy", "Legacy"), ("appium", "Appium")],
                default="legacy",
                max_length=20,
                verbose_name="执行引擎快照",
            ),
        ),
        migrations.AlterField(
            model_name="apptestcase",
            name="engine",
            field=models.CharField(
                choices=[("legacy", "Legacy"), ("appium", "Appium")],
                default=None,
                max_length=20,
                verbose_name="执行引擎",
            ),
        ),
        migrations.AlterField(
            model_name="apptestexecution",
            name="engine",
            field=models.CharField(
                choices=[("legacy", "Legacy"), ("appium", "Appium")],
                default=None,
                max_length=20,
                verbose_name="执行引擎快照",
            ),
        ),
        migrations.AlterField(
            model_name="appelement",
            name="element_type",
            field=models.CharField(
                choices=[
                    ("image", "图片元素"),
                    ("pos", "坐标元素"),
                    ("region", "区域元素"),
                    ("native", "原生元素"),
                ],
                max_length=10,
                verbose_name="元素类型",
            ),
        ),
    ]
