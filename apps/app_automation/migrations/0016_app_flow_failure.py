# Generated for structured Appium flow-failure diagnostics.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("app_automation", "0015_appiumsession_appiumoperation_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="AppFlowFailure",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "code",
                    models.CharField(
                        choices=[
                            ("element_not_found", "Element not found"),
                            ("locator_not_found", "Locator not found"),
                            ("element_ambiguous", "Element locator is ambiguous"),
                            ("locator_ambiguous", "Locator is ambiguous"),
                            ("element_state_mismatch", "Element state mismatch"),
                            ("assertion_failed", "Assertion failed"),
                            ("read_only_business_assertion_failed", "Business assertion failed"),
                            ("device_disconnected", "Device disconnected"),
                            ("appium_read_failed", "Appium screen read failed"),
                            ("appium_outcome_unknown", "Device action outcome is unknown"),
                            ("appium_unavailable", "Appium service unavailable"),
                            ("appium_configuration_invalid", "Appium configuration invalid"),
                            ("appium_client_unavailable", "Appium client unavailable"),
                            ("report_generation_failed", "Report generation failed"),
                            ("execution_failed", "Execution failed"),
                            ("step_failed", "Step failed"),
                        ],
                        max_length=64,
                    ),
                ),
                ("step_name", models.CharField(blank=True, default="", max_length=160)),
                ("step_path", models.CharField(blank=True, default="", max_length=512)),
                ("step_index", models.PositiveIntegerField(blank=True, null=True)),
                ("case_revision", models.CharField(blank=True, default="", max_length=64)),
                ("engine", models.CharField(blank=True, default="", max_length=20)),
                ("duration_ms", models.PositiveBigIntegerField(default=0)),
                ("match_count", models.PositiveIntegerField(blank=True, null=True)),
                ("screenshot_path", models.CharField(blank=True, default="", max_length=1024)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "execution",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="flow_failure",
                        to="app_automation.apptestexecution",
                    ),
                ),
            ],
            options={
                "db_table": "app_flow_failures",
                "ordering": ["created_at"],
            },
        ),
    ]
