"""Structured first-failure diagnostics for Appium flow executions."""

from django.db import models


FLOW_FAILURE_CODE_CHOICES = [
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
]


class AppFlowFailure(models.Model):
    """One immutable, sanitized failure record per execution."""

    execution = models.OneToOneField(
        "AppTestExecution",
        on_delete=models.CASCADE,
        related_name="flow_failure",
    )
    code = models.CharField(max_length=64, choices=FLOW_FAILURE_CODE_CHOICES)
    step_name = models.CharField(max_length=160, blank=True, default="")
    step_path = models.CharField(max_length=512, blank=True, default="")
    step_index = models.PositiveIntegerField(null=True, blank=True)
    case_revision = models.CharField(max_length=64, blank=True, default="")
    engine = models.CharField(max_length=20, blank=True, default="")
    duration_ms = models.PositiveBigIntegerField(default=0)
    match_count = models.PositiveIntegerField(null=True, blank=True)
    screenshot_path = models.CharField(max_length=1024, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "app_flow_failures"
        ordering = ["created_at"]

    def __str__(self):
        return f"Execution {self.execution_id}: {self.code}"
