import apps.executions.models
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("versions", "0001_initial"),
        ("projects", "0002_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("executions", "0002_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExecutionAttempt",
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
                    "attempt_number",
                    models.PositiveIntegerField(default=1, verbose_name="尝试序号"),
                ),
                (
                    "idempotency_key",
                    models.CharField(
                        default=apps.executions.models._new_idempotency_key,
                        max_length=128,
                        verbose_name="幂等键",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("cancel_requested", "Cancel requested"),
                            ("cancelled", "Cancelled"),
                            ("passed", "Passed"),
                            ("succeeded", "Succeeded"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("blocked", "Blocked"),
                            ("unsupported", "Unsupported"),
                        ],
                        default="pending",
                        max_length=32,
                        verbose_name="状态",
                    ),
                ),
                (
                    "worker",
                    models.CharField(
                        blank=True, default="", max_length=255, verbose_name="工作节点"
                    ),
                ),
                (
                    "result",
                    models.JSONField(blank=True, default=dict, verbose_name="结果"),
                ),
                (
                    "error_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("assertion", "Assertion"),
                            ("timeout", "Timeout"),
                            ("configuration", "Configuration"),
                            ("infrastructure", "Infrastructure"),
                            ("permission", "Permission"),
                            ("cancelled", "Cancelled"),
                            ("unsupported", "Unsupported"),
                            ("unknown", "Unknown"),
                        ],
                        default="",
                        max_length=32,
                        verbose_name="错误类型",
                    ),
                ),
                (
                    "error_message",
                    models.TextField(blank=True, default="", verbose_name="错误信息"),
                ),
                (
                    "started_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="开始时间"
                    ),
                ),
                (
                    "finished_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="结束时间"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="创建时间"
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="更新时间"),
                ),
            ],
            options={
                "db_table": "execution_attempts",
                "ordering": ["job_id", "attempt_number"],
            },
        ),
        migrations.CreateModel(
            name="ExecutionStepResult",
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
                    "case_key",
                    models.CharField(max_length=255, verbose_name="用例稳定标识"),
                ),
                (
                    "step_key",
                    models.CharField(max_length=255, verbose_name="步骤稳定标识"),
                ),
                (
                    "idempotency_key",
                    models.CharField(
                        default=apps.executions.models._new_idempotency_key,
                        max_length=128,
                        verbose_name="幂等键",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("passed", "Passed"),
                            ("succeeded", "Succeeded"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("blocked", "Blocked"),
                            ("skipped", "Skipped"),
                        ],
                        default="pending",
                        max_length=32,
                        verbose_name="状态",
                    ),
                ),
                (
                    "result",
                    models.JSONField(blank=True, default=dict, verbose_name="结果"),
                ),
                (
                    "error_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("assertion", "Assertion"),
                            ("timeout", "Timeout"),
                            ("configuration", "Configuration"),
                            ("infrastructure", "Infrastructure"),
                            ("permission", "Permission"),
                            ("cancelled", "Cancelled"),
                            ("unsupported", "Unsupported"),
                            ("unknown", "Unknown"),
                        ],
                        default="",
                        max_length=32,
                        verbose_name="错误类型",
                    ),
                ),
                (
                    "error_message",
                    models.TextField(blank=True, default="", verbose_name="错误信息"),
                ),
                (
                    "duration_ms",
                    models.PositiveIntegerField(
                        blank=True, null=True, verbose_name="耗时毫秒"
                    ),
                ),
                (
                    "started_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="开始时间"
                    ),
                ),
                (
                    "finished_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="结束时间"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="创建时间"
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="更新时间"),
                ),
                (
                    "attempt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="step_results",
                        to="executions.executionattempt",
                        verbose_name="执行尝试",
                    ),
                ),
            ],
            options={
                "db_table": "execution_step_results",
                "ordering": ["attempt_id", "case_key", "step_key"],
            },
        ),
        migrations.CreateModel(
            name="ExecutionJob",
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
                    "source_type",
                    models.CharField(
                        choices=[
                            ("api", "API"),
                            ("ui", "UI"),
                            ("app", "APP"),
                            ("performance", "Performance"),
                            ("manual", "Manual"),
                            ("other", "Other"),
                        ],
                        default="manual",
                        max_length=32,
                        verbose_name="来源类型",
                    ),
                ),
                (
                    "source_id",
                    models.CharField(
                        blank=True, default="", max_length=255, verbose_name="来源标识"
                    ),
                ),
                (
                    "build_reference",
                    models.CharField(
                        blank=True, default="", max_length=255, verbose_name="构建引用"
                    ),
                ),
                (
                    "trigger",
                    models.CharField(
                        choices=[
                            ("manual", "Manual"),
                            ("api", "API"),
                            ("schedule", "Schedule"),
                            ("ci", "CI"),
                            ("retry", "Retry"),
                            ("system", "System"),
                        ],
                        default="manual",
                        max_length=32,
                        verbose_name="触发方式",
                    ),
                ),
                (
                    "environment_snapshot",
                    models.JSONField(blank=True, default=dict, verbose_name="环境快照"),
                ),
                (
                    "priority",
                    models.CharField(
                        choices=[
                            ("low", "Low"),
                            ("normal", "Normal"),
                            ("medium", "Medium"),
                            ("high", "High"),
                            ("critical", "Critical"),
                        ],
                        default="normal",
                        max_length=16,
                        verbose_name="优先级",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("cancel_requested", "Cancel requested"),
                            ("cancelled", "Cancelled"),
                            ("passed", "Passed"),
                            ("succeeded", "Succeeded"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("blocked", "Blocked"),
                            ("unsupported", "Unsupported"),
                        ],
                        default="pending",
                        max_length=32,
                        verbose_name="状态",
                    ),
                ),
                (
                    "cancel_reason",
                    models.TextField(blank=True, default="", verbose_name="取消原因"),
                ),
                (
                    "attempt",
                    models.PositiveIntegerField(default=0, verbose_name="当前尝试次数"),
                ),
                (
                    "worker",
                    models.CharField(
                        blank=True, default="", max_length=255, verbose_name="工作节点"
                    ),
                ),
                (
                    "result",
                    models.JSONField(blank=True, default=dict, verbose_name="结果"),
                ),
                (
                    "error_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("assertion", "Assertion"),
                            ("timeout", "Timeout"),
                            ("configuration", "Configuration"),
                            ("infrastructure", "Infrastructure"),
                            ("permission", "Permission"),
                            ("cancelled", "Cancelled"),
                            ("unsupported", "Unsupported"),
                            ("unknown", "Unknown"),
                        ],
                        default="",
                        max_length=32,
                        verbose_name="错误类型",
                    ),
                ),
                (
                    "error_message",
                    models.TextField(blank=True, default="", verbose_name="错误信息"),
                ),
                (
                    "idempotency_key",
                    models.CharField(
                        default=apps.executions.models._new_idempotency_key,
                        max_length=128,
                        verbose_name="幂等键",
                    ),
                ),
                (
                    "queued_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="排队时间"
                    ),
                ),
                (
                    "started_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="开始时间"
                    ),
                ),
                (
                    "finished_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="结束时间"
                    ),
                ),
                (
                    "cancelled_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="取消时间"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="创建时间"
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="更新时间"),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="execution_jobs",
                        to="projects.project",
                        verbose_name="项目",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="execution_jobs_requested",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="请求用户",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="execution_jobs",
                        to="versions.version",
                        verbose_name="版本",
                    ),
                ),
            ],
            options={
                "db_table": "execution_jobs",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddField(
            model_name="executionattempt",
            name="job",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="attempts",
                to="executions.executionjob",
                verbose_name="执行任务",
            ),
        ),
        migrations.CreateModel(
            name="ExecutionArtifact",
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
                    "idempotency_key",
                    models.CharField(
                        default=apps.executions.models._new_idempotency_key,
                        max_length=128,
                        unique=True,
                        verbose_name="幂等键",
                    ),
                ),
                ("uri", models.CharField(max_length=2048, verbose_name="产物 URI")),
                (
                    "content_hash",
                    models.CharField(
                        blank=True, default="", max_length=128, verbose_name="内容哈希"
                    ),
                ),
                (
                    "media_type",
                    models.CharField(
                        blank=True, default="", max_length=255, verbose_name="媒体类型"
                    ),
                ),
                (
                    "size_bytes",
                    models.PositiveBigIntegerField(
                        blank=True, null=True, verbose_name="大小字节"
                    ),
                ),
                (
                    "retention_policy",
                    models.CharField(
                        choices=[
                            ("default", "Default"),
                            ("short", "Short"),
                            ("long", "Long"),
                            ("forever", "Forever"),
                        ],
                        default="default",
                        max_length=16,
                        verbose_name="保留策略",
                    ),
                ),
                (
                    "retention_until",
                    models.DateTimeField(blank=True, null=True, verbose_name="保留至"),
                ),
                (
                    "metadata",
                    models.JSONField(blank=True, default=dict, verbose_name="元数据"),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="创建时间"
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="更新时间"),
                ),
                (
                    "attempt",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="artifacts",
                        to="executions.executionattempt",
                        verbose_name="执行尝试",
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="artifacts",
                        to="executions.executionjob",
                        verbose_name="执行任务",
                    ),
                ),
                (
                    "step_result",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="artifacts",
                        to="executions.executionstepresult",
                        verbose_name="步骤结果",
                    ),
                ),
            ],
            options={
                "db_table": "execution_artifacts",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="executionstepresult",
            constraint=models.UniqueConstraint(
                fields=("attempt", "case_key", "step_key"),
                name="execution_step_attempt_case_step_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="executionstepresult",
            constraint=models.UniqueConstraint(
                fields=("attempt", "idempotency_key"),
                name="execution_step_attempt_idempotency_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="executionjob",
            constraint=models.UniqueConstraint(
                fields=("project", "idempotency_key"),
                name="execution_job_project_idempotency_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="executionjob",
            constraint=models.CheckConstraint(
                check=models.Q(("attempt__gte", 0)),
                name="execution_job_attempt_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="executionattempt",
            constraint=models.UniqueConstraint(
                fields=("job", "attempt_number"),
                name="execution_attempt_job_number_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="executionattempt",
            constraint=models.UniqueConstraint(
                fields=("job", "idempotency_key"),
                name="execution_attempt_job_idempotency_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="executionattempt",
            constraint=models.CheckConstraint(
                check=models.Q(("attempt_number__gt", 0)),
                name="execution_attempt_number_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="executionartifact",
            constraint=models.UniqueConstraint(
                fields=("attempt", "uri"), name="execution_artifact_attempt_uri_uniq"
            ),
        ),
    ]
