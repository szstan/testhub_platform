"""Durable ownership records for interactive and execution-bound Appium sessions."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class AppiumSession(models.Model):
    STATUS_CHOICES = [
        ("starting", "Starting"),
        ("active", "Active"),
        ("closing", "Closing"),
        ("closed", "Closed"),
        ("failed", "Failed"),
        ("unknown", "Unknown"),
    ]
    KIND_CHOICES = [
        ("interactive", "Interactive recorder"),
        ("execution", "Test execution"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="interactive")
    device = models.ForeignKey(
        "AppDevice", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="appium_sessions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="appium_sessions",
    )
    project = models.ForeignKey(
        "AppProject", on_delete=models.PROTECT, related_name="appium_sessions",
    )
    package = models.ForeignKey(
        "AppPackage", on_delete=models.PROTECT, related_name="appium_sessions",
    )
    execution = models.ForeignKey(
        "AppTestExecution", on_delete=models.PROTECT, null=True, blank=True,
        related_name="appium_sessions",
    )
    lease_generation = models.PositiveBigIntegerField()
    execution_generation = models.PositiveBigIntegerField(null=True, blank=True)
    device_action_epoch = models.PositiveBigIntegerField()
    system_port = models.PositiveIntegerField()
    appium_endpoint = models.CharField(max_length=255)
    remote_session_id = models.CharField(max_length=255, blank=True, default="")
    capabilities = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="starting")
    request_key = models.CharField(max_length=64)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    parent_pid = models.PositiveIntegerField(null=True, blank=True)
    parent_start_token = models.CharField(max_length=64, blank=True, default="")
    parent_cleanup_proven_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "appium_sessions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["device", "status"], name="appium_device_status_idx"),
            models.Index(fields=["user", "status"], name="appium_user_status_idx"),
            models.Index(fields=["execution", "execution_generation"], name="appium_exec_gen_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["device", "user", "request_key"],
                name="appium_session_request_key_uniq",
            ),
        ]

    @property
    def active(self):
        return self.status in {"starting", "active", "closing", "unknown"}


class AppiumOperation(models.Model):
    ACTION_CHOICES = [
        ("start", "Start session"),
        ("screenshot", "Capture screenshot"),
        ("tree", "Read UI hierarchy"),
        ("locate", "Locate native element"),
        ("click", "Record native click"),
        ("close", "Close session"),
    ]
    STATUS_CHOICES = [
        ("queued", "Queued"),
        ("running", "Running"),
        ("succeeded", "Succeeded"),
        ("failed", "Failed"),
        ("unknown", "Unknown"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        AppiumSession, on_delete=models.PROTECT, related_name="operations",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="appium_operations",
    )
    lease_generation = models.PositiveBigIntegerField()
    device_action_epoch = models.PositiveBigIntegerField()
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued")
    request_key = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    dispatched_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "appium_operations"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["session", "status"], name="appium_op_session_status_idx"),
            models.Index(fields=["user", "created_at"], name="appium_op_user_created_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "request_key"], name="appium_op_request_key_uniq",
            ),
        ]

    def mark_finished(self, status, *, error_code=""):
        self.status = status
        self.error_code = error_code
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "error_code", "finished_at", "updated_at"])
