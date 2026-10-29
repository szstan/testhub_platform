from django.db import models
from django.utils import timezone
from apps.users.models import User
from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.versions.models import Version
from apps.requirement_analysis.models import BusinessRequirement
from apps.defects.models import Defect

class TestPlan(models.Model):
    """测试计划"""
    name = models.CharField(max_length=200, verbose_name='计划名称')
    description = models.TextField(blank=True, verbose_name='计划描述')
    projects = models.ManyToManyField(Project, blank=True, related_name='test_plans', verbose_name='关联项目')
    version = models.ForeignKey(Version, on_delete=models.CASCADE, null=True, blank=True, related_name='test_plans', verbose_name='关联版本')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_plans', verbose_name='创建者')
    assignees = models.ManyToManyField(User, blank=True, related_name='assigned_plans', verbose_name='指派给')
    is_active = models.BooleanField(default=True, verbose_name='是否激活')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'test_plans'
        verbose_name = '测试计划'
        verbose_name_plural = '测试计划'
        ordering = ['-created_at']

class TestRun(models.Model):
    """测试执行"""
    STATUS_CHOICES = [
        ('untested', '未测试'),
        ('in_progress', '进行中'),
        ('completed', '已完成'),
        ('blocked', '阻塞'),
    ]
    
    name = models.CharField(max_length=200, verbose_name='执行名称')
    description = models.TextField(blank=True, verbose_name='执行描述')
    test_plan = models.ForeignKey(TestPlan, on_delete=models.CASCADE, related_name='test_runs', verbose_name='测试计划')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='test_runs', verbose_name='关联项目')
    version = models.ForeignKey(Version, on_delete=models.CASCADE, null=True, blank=True, related_name='test_runs', verbose_name='关联版本')
    testcases = models.ManyToManyField(TestCase, through='TestRunCase', related_name='test_runs', verbose_name='测试用例')
    assignee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='assigned_runs', verbose_name='执行人')
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_runs', verbose_name='创建者')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='untested', verbose_name='状态')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='完成时间')
    due_date = models.DateTimeField(null=True, blank=True, verbose_name='截止日期')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'test_runs'
        verbose_name = '测试执行'
        verbose_name_plural = '测试执行'
        ordering = ['-created_at']
    
    @property
    def progress_stats(self):
        """执行进度统计"""
        total = self.run_cases.count()
        if total == 0:
            return {'total': 0, 'untested': 0, 'passed': 0, 'failed': 0, 'blocked': 0, 'retest': 0, 'progress': 0}
        
        stats = {
            'total': total,
            'untested': self.run_cases.filter(status='untested').count(),
            'passed': self.run_cases.filter(status='passed').count(),
            'failed': self.run_cases.filter(status='failed').count(),
            'blocked': self.run_cases.filter(status='blocked').count(),
            'retest': self.run_cases.filter(status='retest').count(),
        }
        stats['tested'] = stats['passed'] + stats['failed'] + stats['blocked'] + stats['retest']
        stats['progress'] = round((stats['tested'] / total) * 100, 1) if total > 0 else 0
        return stats

class TestRunCase(models.Model):
    """测试执行用例"""
    STATUS_CHOICES = [
        ('untested', '未测试'),
        ('passed', '通过'),
        ('failed', '失败'),
        ('blocked', '阻塞'),
        ('retest', '重测'),
    ]
    
    PRIORITY_CHOICES = [
        ('low', '低'),
        ('medium', '中'),
        ('high', '高'),
        ('critical', '紧急'),
    ]
    
    test_run = models.ForeignKey(TestRun, on_delete=models.CASCADE, related_name='run_cases', verbose_name='测试执行')
    testcase = models.ForeignKey(TestCase, on_delete=models.CASCADE, verbose_name='测试用例')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='untested', verbose_name='执行状态')
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='medium', verbose_name='优先级')
    actual_result = models.TextField(blank=True, verbose_name='实际结果')
    comments = models.TextField(blank=True, verbose_name='备注')
    defects = models.JSONField(default=list, verbose_name='关联缺陷')  # 存储缺陷ID列表
    elapsed_time = models.DurationField(null=True, blank=True, verbose_name='执行耗时')
    executed_by = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, verbose_name='执行者')
    executed_at = models.DateTimeField(null=True, blank=True, verbose_name='执行时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        db_table = 'test_run_cases'
        unique_together = ['test_run', 'testcase']
        verbose_name = '测试执行用例'
        verbose_name_plural = '测试执行用例'
        ordering = ['testcase__id']

class TestRunCaseHistory(models.Model):
    """测试执行历史"""
    run_case = models.ForeignKey(TestRunCase, on_delete=models.CASCADE, related_name='history', verbose_name='执行用例')
    status = models.CharField(max_length=20, choices=TestRunCase.STATUS_CHOICES, verbose_name='执行状态')
    actual_result = models.TextField(blank=True, verbose_name='实际结果')
    comments = models.TextField(blank=True, verbose_name='备注')
    executed_by = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name='执行者')
    executed_at = models.DateTimeField(default=timezone.now, verbose_name='执行时间')
    
    class Meta:
        db_table = 'test_run_case_history'
        verbose_name = '测试执行历史'
        verbose_name_plural = '测试执行历史'
        ordering = ['-executed_at']


# ---------------------------------------------------------------------------
# Unified execution domain (additive to the legacy TestPlan/TestRun models)
# ---------------------------------------------------------------------------

import hashlib
import uuid
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.db.models import Q

from .state_machine import (
    ATTEMPT_TRANSITIONS,
    ErrorType,
    ExecutionStatus,
    JOB_TRANSITIONS,
    STEP_TRANSITIONS,
    AttemptStatus,
    StepStatus,
    InvalidExecutionTransition,
    validate_transition,
)


def _new_idempotency_key():
    return str(uuid4())


def _new_evaluation_key():
    return str(uuid4())


EXECUTION_SOURCE_CHOICES = (

    ('api', 'API'),
    ('ui', 'UI'),
    ('app', 'APP'),
    ('performance', 'Performance'),
    ('ci', 'CI'),
    ('manual', 'Manual'),
    ('other', 'Other'),
)

EXECUTION_TRIGGER_CHOICES = (
    ('manual', 'Manual'),
    ('api', 'API'),
    ('schedule', 'Schedule'),
    ('ci', 'CI'),
    ('retry', 'Retry'),
    ('system', 'System'),
)

EXECUTION_PRIORITY_CHOICES = (
    ('low', 'Low'),
    ('normal', 'Normal'),
    ('medium', 'Medium'),
    ('high', 'High'),
    ('critical', 'Critical'),
)

RETENTION_POLICY_CHOICES = (
    ('default', 'Default'),
    ('short', 'Short'),
    ('long', 'Long'),
    ('forever', 'Forever'),
)


class IdempotencyConflict(ValueError):
    """Raised when one idempotency key is reused for another request payload."""


class ExecutionJobManager(models.Manager):
    """Concurrency-safe job creation for API and future adapters."""

    _PAYLOAD_FIELDS = (
        'source_type',
        'profile_id',
        'source_id',
        'version_id',
        'build_id',
        'build_reference',
        'trigger',
        'environment_snapshot',
        'priority',
        'dataset_id',
    )

    def create_idempotent(self, *, project, idempotency_key=None, requested_by=None, defaults=None):
        idempotency_key = (idempotency_key or '').strip() or str(uuid4())
        values = dict(defaults or {})
        values.pop('project', None)
        values.pop('idempotency_key', None)
        if 'version' in values and 'version_id' not in values:
            version = values['version']
            values['version_id'] = version.pk if version is not None else None
        if 'build' in values and 'build_id' not in values:
            build = values['build']
            values['build_id'] = build.pk if build is not None else None
        if 'profile' in values and 'profile_id' not in values:
            profile = values['profile']
            values['profile_id'] = profile.pk if profile is not None else None
        if 'dataset' in values and 'dataset_id' not in values:
            dataset = values['dataset']
            values['dataset_id'] = dataset.pk if dataset is not None else None
        values.setdefault('requested_by', requested_by)
        values.setdefault('status', ExecutionStatus.PENDING)

        with transaction.atomic():
            existing = self.select_for_update().filter(
                project=project,
                idempotency_key=idempotency_key,
            ).first()
            if existing is not None:
                self._assert_same_payload(existing, values)
                return existing, False

            try:
                create_values = dict(values)
                if 'version' in create_values and 'version_id' in create_values:
                    create_values.pop('version_id')
                if 'build' in create_values and 'build_id' in create_values:
                    create_values.pop('build_id')
                if 'profile' in create_values and 'profile_id' in create_values:
                    create_values.pop('profile_id')
                return self.create(
                    project=project,
                    idempotency_key=idempotency_key,
                    **create_values,
                ), True
            except IntegrityError:
                # A concurrent request may have committed the unique key after
                # the initial lookup.  Read it back and apply the same payload
                # check instead of creating a second job.
                existing = self.select_for_update().get(
                    project=project,
                    idempotency_key=idempotency_key,
                )
                self._assert_same_payload(existing, values)
                return existing, False

    def _assert_same_payload(self, existing, values):
        for field in self._PAYLOAD_FIELDS:
            if field not in values:
                continue
            expected = values[field]
            if field == 'version_id':
                actual = existing.version_id
            else:
                actual = getattr(existing, field)
            if actual != expected:
                raise IdempotencyConflict(
                    'Idempotency key was already used with a different request payload'
                )


class ExecutionJob(models.Model):
    """A durable, executor-neutral request to run a source artifact."""

    STATUS_CHOICES = ExecutionStatus.choices
    SOURCE_TYPE_CHOICES = EXECUTION_SOURCE_CHOICES
    TRIGGER_CHOICES = EXECUTION_TRIGGER_CHOICES
    PRIORITY_CHOICES = EXECUTION_PRIORITY_CHOICES
    ERROR_TYPE_CHOICES = ErrorType.choices

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='execution_jobs',
        verbose_name='项目',
    )
    dataset = models.ForeignKey(
        'ExecutionDataset',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_jobs',
        verbose_name='数据集',
    )
    profile = models.ForeignKey(
        'ExecutionProfile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_jobs',
        verbose_name='执行配置',
    )
    source_type = models.CharField(
        max_length=32,
        choices=SOURCE_TYPE_CHOICES,
        default='manual',
        verbose_name='来源类型',
    )
    source_id = models.CharField(max_length=255, blank=True, default='', verbose_name='来源标识')
    version = models.ForeignKey(
        Version,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_jobs',
        verbose_name='版本',
    )
    build = models.ForeignKey(
        'ExecutionBuild',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_jobs',
        verbose_name='构建',
    )
    build_reference = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name='构建引用',
    )
    trigger = models.CharField(
        max_length=32,
        choices=TRIGGER_CHOICES,
        default='manual',
        verbose_name='触发方式',
    )
    environment_snapshot = models.JSONField(default=dict, blank=True, verbose_name='环境快照')
    dataset_snapshot = models.JSONField(default=dict, blank=True, verbose_name='数据集脱敏快照')
    priority = models.CharField(
        max_length=16,
        choices=PRIORITY_CHOICES,
        default='normal',
        verbose_name='优先级',
    )
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=ExecutionStatus.PENDING,
        verbose_name='状态',
    )
    cancel_reason = models.TextField(blank=True, default='', verbose_name='取消原因')
    attempt = models.PositiveIntegerField(default=0, verbose_name='当前尝试次数')
    worker = models.CharField(max_length=255, blank=True, default='', verbose_name='工作节点')
    requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_jobs_requested',
        verbose_name='请求用户',
    )
    result = models.JSONField(default=dict, blank=True, verbose_name='结果')
    final_result = models.JSONField(default=dict, blank=True, verbose_name='最终结果')
    final_attempt = models.ForeignKey(
        'ExecutionAttempt',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='finalized_jobs',
        verbose_name='最终尝试',
    )
    final_status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        blank=True,
        default='',
        verbose_name='最终状态',
    )
    quality_gate_result = models.JSONField(default=dict, blank=True, verbose_name='质量门禁结果')
    retry_policy_snapshot = models.JSONField(default=dict, blank=True, verbose_name='重试策略快照')
    finalized_at = models.DateTimeField(null=True, blank=True, verbose_name='最终化时间')
    error_type = models.CharField(
        max_length=32,
        choices=ERROR_TYPE_CHOICES,
        blank=True,
        default='',
        verbose_name='错误类型',
    )
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    idempotency_key = models.CharField(
        max_length=128,
        default=_new_idempotency_key,
        verbose_name='幂等键',
    )
    queued_at = models.DateTimeField(null=True, blank=True, verbose_name='排队时间')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name='取消时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    objects = ExecutionJobManager()

    class Meta:
        db_table = 'execution_jobs'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=('project', 'idempotency_key'),
                name='execution_job_project_idempotency_uniq',
            ),
            models.CheckConstraint(
                check=Q(attempt__gte=0),
                name='execution_job_attempt_nonnegative',
            ),
        ]

    def __str__(self):
        return f'ExecutionJob({self.pk})'

    def initialize_dataset_variants(self):
        """Freeze a redacted dataset snapshot and create deterministic variants."""
        if not self.dataset_id:
            return []

        from .datasets import expand_dataset
        from .result_events import redact_result_payload

        dataset = self.dataset
        variants = expand_dataset(dataset)
        snapshot = redact_result_payload({
            'dataset_id': dataset.pk,
            'name': dataset.name,
            'description': dataset.description,
            'data': dataset.data or [],
            'matrix': dataset.matrix or {},
            'secret_refs': dataset.secret_refs or [],
            'variant_count': len(variants),
        })
        if self.dataset_snapshot and self.dataset_snapshot != snapshot:
            raise ValueError('Execution job dataset snapshot is immutable')
        if not self.dataset_snapshot:
            self.dataset_snapshot = snapshot
            self.save(update_fields=['dataset_snapshot', 'updated_at'])

        created = []
        for ordinal, payload in enumerate(variants):
            variant, was_created = ExecutionJobVariant.objects.get_or_create(
                job=self,
                ordinal=ordinal,
                defaults={
                    'variant_key': f'variant-{ordinal + 1}',
                    'payload': redact_result_payload(dict(payload)),
                },
            )
            if variant.payload != redact_result_payload(dict(payload)):
                raise ValueError('Execution job variant payload is immutable')
            created.append(variant)
        return created

    def dataset_variants(self):
        if not self.dataset_id:
            return self.__class__._meta.apps.get_model('executions', 'ExecutionJobVariant').objects.none()
        return self.variants.order_by('ordinal')

    @property
    def creator(self):
        """Compatibility alias for callers that call the requester a creator."""
        return self.requested_by

    @property
    def build_id(self):
        return self.build_reference

    @build_id.setter
    def build_id(self, value):
        self.build_reference = value or ''

    @property
    def build_ref(self):
        return self.build_reference

    @build_ref.setter
    def build_ref(self, value):
        self.build_reference = value or ''

    @property
    def worker_id(self):
        return self.worker

    @worker_id.setter
    def worker_id(self, value):
        self.worker = value or ''

    @property
    def is_terminal(self):
        return self.status in {
            ExecutionStatus.CANCELLED,
            ExecutionStatus.PASSED,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.BLOCKED,
            ExecutionStatus.UNSUPPORTED,
        }

    def transition_to(self, new_status, *, reason=None, save=True):
        target = validate_transition(self.status, new_status, JOB_TRANSITIONS)
        changed_fields = []
        if target != self.status:
            self.status = target
            changed_fields.append('status')
        now = timezone.now()
        if target == ExecutionStatus.QUEUED and self.queued_at is None:
            self.queued_at = now
            changed_fields.append('queued_at')
        if target == ExecutionStatus.RUNNING and self.started_at is None:
            self.started_at = now
            changed_fields.append('started_at')
        if target in {
            ExecutionStatus.CANCELLED,
            ExecutionStatus.PASSED,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.BLOCKED,
            ExecutionStatus.UNSUPPORTED,
        } and self.finished_at is None:
            self.finished_at = now
            changed_fields.append('finished_at')
        if target == ExecutionStatus.CANCELLED and self.cancelled_at is None:
            self.cancelled_at = now
            changed_fields.append('cancelled_at')
        if reason is not None and reason != self.cancel_reason:
            self.cancel_reason = reason
            changed_fields.append('cancel_reason')
        if save and changed_fields:
            changed_fields.append('updated_at')
            self.save(update_fields=changed_fields)
        return self

    def request_cancel(self, reason=''):
        if self.status == ExecutionStatus.CANCEL_REQUESTED:
            if reason and reason != self.cancel_reason:
                self.cancel_reason = reason
                self.save(update_fields=['cancel_reason', 'updated_at'])
            return self
        return self.transition_to(
            ExecutionStatus.CANCEL_REQUESTED,
            reason=reason or self.cancel_reason,
        )

    def create_attempt(self, *, attempt_number=None, idempotency_key=None, **defaults):
        """Create or replay an attempt while serializing the job counter."""
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if attempt_number is None:
                latest = locked.attempts.order_by('-attempt_number').values_list(
                    'attempt_number', flat=True
                ).first()
                attempt_number = (latest or 0) + 1
            attempt, created = ExecutionAttempt.objects.get_or_create(
                job=locked,
                attempt_number=attempt_number,
                defaults={
                    'idempotency_key': idempotency_key or str(uuid4()),
                    **defaults,
                },
            )
            if locked.attempt < attempt_number:
                locked.attempt = attempt_number
                locked.save(update_fields=['attempt', 'updated_at'])
            return attempt, created


class ExecutionSourceBinding(models.Model):
    """Explicit project-scoped binding from a unified source to a legacy runner."""

    SOURCE_TYPE_CHOICES = (
        ('api', 'API'),
        ('ui', 'UI'),
        ('app', 'APP'),
        ('performance', 'Performance'),
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='execution_source_bindings',
        verbose_name='统一项目',
    )
    source_type = models.CharField(max_length=32, choices=SOURCE_TYPE_CHOICES)
    source_id = models.CharField(max_length=255)
    source_project_id = models.CharField(max_length=255, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_source_bindings_created',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_source_bindings'
        constraints = [
            models.UniqueConstraint(
                fields=('project', 'source_type', 'source_id'),
                name='execution_binding_project_source_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=('source_type', 'source_id')),
            models.Index(fields=('project', 'enabled')),
        ]


class ExecutionAttempt(models.Model):
    """One retryable execution attempt for a job."""

    STATUS_CHOICES = AttemptStatus.choices
    ERROR_TYPE_CHOICES = ErrorType.choices

    job = models.ForeignKey(
        ExecutionJob,
        on_delete=models.CASCADE,
        related_name='attempts',
        verbose_name='执行任务',
    )
    variant = models.ForeignKey(
        'ExecutionJobVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='attempts',
        verbose_name='数据集变体',
    )
    attempt_number = models.PositiveIntegerField(default=1, verbose_name='尝试序号')
    idempotency_key = models.CharField(
        max_length=128,
        default=_new_idempotency_key,
        verbose_name='幂等键',
    )
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=AttemptStatus.PENDING,
        verbose_name='状态',
    )
    worker = models.CharField(max_length=255, blank=True, default='', verbose_name='工作节点')
    result = models.JSONField(default=dict, blank=True, verbose_name='结果')
    retry_reason = models.TextField(blank=True, default='', verbose_name='重试原因')
    retry_requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_attempt_retries_requested',
        verbose_name='重试请求人',
    )
    retry_requested_at = models.DateTimeField(null=True, blank=True, verbose_name='重试请求时间')
    retry_policy = models.ForeignKey(
        'ExecutionRetryPolicy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='attempts',
        verbose_name='重试策略',
    )
    retry_mode = models.CharField(max_length=16, blank=True, default='', verbose_name='重试模式')
    retry_decision = models.CharField(max_length=64, blank=True, default='', verbose_name='重试决策')
    retry_scheduled_at = models.DateTimeField(null=True, blank=True, verbose_name='计划重试时间')
    is_final = models.BooleanField(default=False, verbose_name='是否最终尝试')
    error_type = models.CharField(
        max_length=32,
        choices=ERROR_TYPE_CHOICES,
        blank=True,
        default='',
        verbose_name='错误类型',
    )
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    dispatch_id = models.CharField(max_length=255, blank=True, default='', verbose_name='调度标识')
    worker_claim_id = models.CharField(max_length=255, blank=True, default='', verbose_name='worker claim')
    worker_claimed_at = models.DateTimeField(null=True, blank=True, verbose_name='worker claim 时间')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'execution_attempts'
        ordering = ['job_id', 'attempt_number']
        constraints = [
            models.UniqueConstraint(
                fields=('job', 'attempt_number'),
                name='execution_attempt_job_number_uniq',
            ),
            models.UniqueConstraint(
                fields=('job', 'idempotency_key'),
                name='execution_attempt_job_idempotency_uniq',
            ),
            models.CheckConstraint(
                check=Q(attempt_number__gt=0),
                name='execution_attempt_number_positive',
            ),
        ]

    @property
    def attempt(self):
        return self.attempt_number

    @property
    def worker_id(self):
        return self.worker

    @property
    def is_terminal(self):
        return self.status in {
            AttemptStatus.CANCELLED,
            AttemptStatus.PASSED,
            AttemptStatus.SUCCEEDED,
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.BLOCKED,
            AttemptStatus.UNSUPPORTED,
        }

    def transition_to(self, new_status, *, save=True):
        target = validate_transition(self.status, new_status, ATTEMPT_TRANSITIONS)
        changed_fields = []
        if target != self.status:
            self.status = target
            changed_fields.append('status')
        now = timezone.now()
        if target == AttemptStatus.RUNNING and self.started_at is None:
            self.started_at = now
            changed_fields.append('started_at')
        if target in {
            AttemptStatus.CANCELLED,
            AttemptStatus.PASSED,
            AttemptStatus.SUCCEEDED,
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.BLOCKED,
            AttemptStatus.UNSUPPORTED,
        } and self.finished_at is None:
            self.finished_at = now
            changed_fields.append('finished_at')
        if save and changed_fields:
            changed_fields.append('updated_at')
            self.save(update_fields=changed_fields)
        return self


class ExecutionJobVariant(models.Model):
    """One immutable, redacted dataset variant within a unified job."""

    STATUS_CHOICES = ExecutionStatus.choices

    job = models.ForeignKey(
        ExecutionJob,
        on_delete=models.CASCADE,
        related_name='variants',
        verbose_name='执行任务',
    )
    variant_key = models.CharField(max_length=128, verbose_name='变体标识')
    ordinal = models.PositiveIntegerField(verbose_name='变体序号')
    payload = models.JSONField(default=dict, blank=True, verbose_name='脱敏参数')
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=ExecutionStatus.PENDING,
        verbose_name='状态',
    )
    result = models.JSONField(default=dict, blank=True, verbose_name='结果')
    error_type = models.CharField(
        max_length=32,
        choices=ErrorType.choices,
        blank=True,
        default='',
        verbose_name='错误类型',
    )
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    dispatch_id = models.CharField(max_length=255, blank=True, default='', verbose_name='调度标识')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'execution_job_variants'
        ordering = ['job_id', 'ordinal']
        constraints = [
            models.UniqueConstraint(
                fields=('job', 'variant_key'),
                name='execution_job_variant_key_uniq',
            ),
            models.UniqueConstraint(
                fields=('job', 'ordinal'),
                name='execution_job_variant_ordinal_uniq',
            ),
        ]

    @property
    def latest_attempt(self):
        return self.attempts.order_by('-attempt_number').first()


class ExecutionRetryRequest(models.Model):
    """Transactional outbox row for an automatic or manual retry dispatch."""

    STATUS_CHOICES = (
        ('queued', 'Queued'),
        ('dispatched', 'Dispatched'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    )

    job = models.ForeignKey(ExecutionJob, on_delete=models.CASCADE, related_name='retry_requests')
    from_attempt = models.ForeignKey(
        ExecutionAttempt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='retry_requests_from',
    )
    to_attempt = models.ForeignKey(
        ExecutionAttempt,
        on_delete=models.CASCADE,
        related_name='retry_requests_to',
    )
    policy = models.ForeignKey(
        'ExecutionRetryPolicy',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='retry_requests',
    )
    idempotency_key = models.CharField(max_length=128, unique=True)
    reason = models.TextField(blank=True, default='')
    automatic = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='queued')
    scheduled_at = models.DateTimeField(default=timezone.now)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_retry_requests'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=('job', 'status', '-created_at'), name='exec_retry_req_job_idx'),
            models.Index(fields=('status', 'scheduled_at'), name='exec_retry_req_due_idx'),
        ]


class ExecutionStepResult(models.Model):
    """Stable result for one case/step within an execution attempt."""

    STATUS_CHOICES = StepStatus.choices
    ERROR_TYPE_CHOICES = ErrorType.choices

    attempt = models.ForeignKey(
        ExecutionAttempt,
        on_delete=models.CASCADE,
        related_name='step_results',
        verbose_name='执行尝试',
    )
    case_key = models.CharField(max_length=255, verbose_name='用例稳定标识')
    step_key = models.CharField(max_length=255, verbose_name='步骤稳定标识')
    idempotency_key = models.CharField(
        max_length=128,
        default=_new_idempotency_key,
        verbose_name='幂等键',
    )
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=StepStatus.PENDING,
        verbose_name='状态',
    )
    result = models.JSONField(default=dict, blank=True, verbose_name='结果')
    error_type = models.CharField(
        max_length=32,
        choices=ERROR_TYPE_CHOICES,
        blank=True,
        default='',
        verbose_name='错误类型',
    )
    error_message = models.TextField(blank=True, default='', verbose_name='错误信息')
    duration_ms = models.PositiveIntegerField(null=True, blank=True, verbose_name='耗时毫秒')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'execution_step_results'
        ordering = ['attempt_id', 'case_key', 'step_key']
        constraints = [
            models.UniqueConstraint(
                fields=('attempt', 'case_key', 'step_key'),
                name='execution_step_attempt_case_step_uniq',
            ),
            models.UniqueConstraint(
                fields=('attempt', 'idempotency_key'),
                name='execution_step_attempt_idempotency_uniq',
            ),
        ]
        indexes = [
            models.Index(
                fields=('attempt', 'case_key', 'status', '-created_at'),
                name='exec_step_case_status_idx',
            ),
        ]

    def transition_to(self, new_status, *, save=True):
        target = validate_transition(self.status, new_status, STEP_TRANSITIONS)
        changed_fields = []
        if target != self.status:
            self.status = target
            changed_fields.append('status')
        now = timezone.now()
        if target == StepStatus.RUNNING and self.started_at is None:
            self.started_at = now
            changed_fields.append('started_at')
        if target in {
            StepStatus.PASSED,
            StepStatus.SUCCEEDED,
            StepStatus.COMPLETED,
            StepStatus.FAILED,
            StepStatus.BLOCKED,
            StepStatus.SKIPPED,
        } and self.finished_at is None:
            self.finished_at = now
            changed_fields.append('finished_at')
        if save and changed_fields:
            changed_fields.append('updated_at')
            self.save(update_fields=changed_fields)
        return self


class ExecutionArtifact(models.Model):
    """A retained URI/hash reference produced by an attempt or step."""

    RETENTION_POLICY_CHOICES = RETENTION_POLICY_CHOICES

    job = models.ForeignKey(
        ExecutionJob,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='artifacts',
        verbose_name='执行任务',
    )
    attempt = models.ForeignKey(
        ExecutionAttempt,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='artifacts',
        verbose_name='执行尝试',
    )
    step_result = models.ForeignKey(
        ExecutionStepResult,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='artifacts',
        verbose_name='步骤结果',
    )
    idempotency_key = models.CharField(
        max_length=128,
        unique=True,
        default=_new_idempotency_key,
        verbose_name='幂等键',
    )
    uri = models.CharField(max_length=2048, verbose_name='产物 URI')
    uri_hash = models.CharField(
        max_length=64,
        editable=False,
        default='',
        verbose_name='产物 URI 哈希',
    )
    content_hash = models.CharField(max_length=128, blank=True, default='', verbose_name='内容哈希')
    media_type = models.CharField(max_length=255, blank=True, default='', verbose_name='媒体类型')
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True, verbose_name='大小字节')
    retention_policy = models.CharField(
        max_length=16,
        choices=RETENTION_POLICY_CHOICES,
        default='default',
        verbose_name='保留策略',
    )
    retention_until = models.DateTimeField(null=True, blank=True, verbose_name='保留至')
    metadata = models.JSONField(default=dict, blank=True, verbose_name='元数据')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'execution_artifacts'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=('retention_until', 'created_at'), name='exec_artifact_retention_idx'),
            models.Index(fields=('job', '-created_at'), name='exec_artifact_job_created_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=('attempt', 'uri_hash'),
                name='execution_artifact_attempt_uri_uniq',
            ),
        ]

    @property
    def artifact_uri(self):
        return self.uri

    @property
    def artifact_hash(self):
        return self.content_hash

    @property
    def retention(self):
        return self.retention_policy

    def save(self, *args, **kwargs):
        # Keep a fixed-width digest for the attempt/URI uniqueness constraint.
        self.uri_hash = hashlib.sha256(self.uri.encode('utf-8')).hexdigest()
        # Adapters may attach an artifact at either job or attempt level.  Keep
        # the denormalized job link populated when an attempt is supplied.
        if self.attempt_id and not self.job_id:
            self.job_id = self.attempt.job_id
        if self.step_result_id:
            if not self.attempt_id:
                self.attempt_id = self.step_result.attempt_id
            if not self.job_id:
                self.job_id = self.step_result.attempt.job_id
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Traceability domain (additive to legacy execution and defect fields)
# ---------------------------------------------------------------------------


class ExecutionBuild(models.Model):
    """A reproducible build/release reference used by test executions."""

    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('passed', 'Passed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='execution_builds', verbose_name='项目')
    version = models.ForeignKey(Version, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_builds', verbose_name='版本')
    name = models.CharField(max_length=200, verbose_name='构建名称')
    build_number = models.CharField(max_length=128, verbose_name='构建号')
    branch = models.CharField(max_length=255, blank=True, default='', verbose_name='分支')
    commit_sha = models.CharField(max_length=128, blank=True, default='', verbose_name='提交 SHA')
    environment = models.CharField(max_length=128, blank=True, default='', verbose_name='环境')
    artifact_url = models.URLField(blank=True, default='', verbose_name='产物地址')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='状态')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_builds_created', verbose_name='创建者')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'execution_builds'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=('project', 'build_number'), name='execution_build_project_number_uniq'),
        ]

    def __str__(self):
        return f'{self.project_id}:{self.build_number}'


class RequirementTestCaseLink(models.Model):
    requirement = models.ForeignKey(BusinessRequirement, on_delete=models.CASCADE, related_name='manual_testcase_links')
    test_case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name='requirement_links')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='requirement_testcase_links_created')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'requirement_testcase_links'
        constraints = [
            models.UniqueConstraint(fields=('requirement', 'test_case'), name='requirement_testcase_link_uniq'),
        ]


class TestCaseDefectLink(models.Model):
    test_case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name='defect_links')
    defect = models.ForeignKey(Defect, on_delete=models.CASCADE, related_name='testcase_links')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='testcase_defect_links_created')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'testcase_defect_links'
        constraints = [
            models.UniqueConstraint(fields=('test_case', 'defect'), name='testcase_defect_link_uniq'),
        ]


class ExecutionRequirementLink(models.Model):
    job = models.ForeignKey(ExecutionJob, on_delete=models.CASCADE, related_name='requirement_links')
    requirement = models.ForeignKey(BusinessRequirement, on_delete=models.CASCADE, related_name='execution_links')
    relation_type = models.CharField(max_length=32, default='covered')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'execution_requirement_links'
        constraints = [
            models.UniqueConstraint(fields=('job', 'requirement'), name='execution_requirement_link_uniq'),
        ]


class ExecutionDefectLink(models.Model):
    job = models.ForeignKey(ExecutionJob, on_delete=models.CASCADE, related_name='defect_links')
    defect = models.ForeignKey(Defect, on_delete=models.CASCADE, related_name='execution_links')
    relation_type = models.CharField(max_length=32, default='observed')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'execution_defect_links'
        constraints = [
            models.UniqueConstraint(fields=('job', 'defect'), name='execution_defect_link_uniq'),
        ]


class TestCaseRevision(models.Model):
    test_case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name='revisions')
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='testcase_revisions_changed')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'testcase_revisions'
        ordering = ['test_case_id', '-revision']
        constraints = [
            models.UniqueConstraint(fields=('test_case', 'revision'), name='testcase_revision_number_uniq'),
        ]

class ExecutionEvent(models.Model):

    """Idempotent execution lifecycle event retained for history and replay."""

    EVENT_TYPES = (
        ('run_started', 'Run started'),
        ('case_started', 'Case started'),
        ('step_started', 'Step started'),
        ('step_finished', 'Step finished'),
        ('case_finished', 'Case finished'),
        ('run_finished', 'Run finished'),
        ('artifact_uploaded', 'Artifact uploaded'),
    )

    job = models.ForeignKey(ExecutionJob, on_delete=models.CASCADE, related_name='events')
    attempt = models.ForeignKey(ExecutionAttempt, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')
    event_type = models.CharField(max_length=32, choices=EVENT_TYPES)
    event_key = models.CharField(max_length=255, unique=True)
    idempotency_key = models.CharField(max_length=128, unique=True)
    case_key = models.CharField(max_length=255, blank=True, default='')
    step_key = models.CharField(max_length=255, blank=True, default='')
    status = models.CharField(max_length=32, blank=True, default='')
    payload = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'execution_events'
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['job', 'created_at'], name='exec_event_job_created_idx'),
            models.Index(fields=['event_type', 'created_at'], name='exec_event_type_created_idx'),
        ]


class ExecutionDataset(models.Model):
    """Project-scoped parameter data with optional secret references."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='execution_datasets')
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True, default='')
    data = models.JSONField(default=list, blank=True)
    matrix = models.JSONField(default=dict, blank=True)
    secret_refs = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_datasets_created')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_datasets'
        ordering = ['project_id', 'name']
        constraints = [
            models.UniqueConstraint(fields=('project', 'name'), name='execution_dataset_project_name_uniq'),
        ]


class ExecutionFixture(models.Model):
    STATUS_CHOICES = (
        ('created', 'Created'),
        ('cleaning', 'Cleaning'),
        ('cleaned', 'Cleaned'),
        ('skipped', 'Skipped'),
        ('failed', 'Failed'),
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='execution_fixtures')
    dataset = models.ForeignKey(ExecutionDataset, on_delete=models.SET_NULL, null=True, blank=True, related_name='fixtures')
    run_id = models.CharField(max_length=128)
    references = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='created')
    cleanup_required = models.BooleanField(default=True)
    cleanup_error = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_fixtures_created')
    created_at = models.DateTimeField(default=timezone.now)
    cleaned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'execution_fixtures'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=('project', 'run_id'), name='execution_fixture_project_run_uniq'),
        ]


class ExecutionDataMatrix(models.Model):
    dataset = models.ForeignKey(ExecutionDataset, on_delete=models.CASCADE, related_name='matrices')
    name = models.CharField(max_length=160)
    columns = models.JSONField(default=list, blank=True)
    rows = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_data_matrices'
        constraints = [
            models.UniqueConstraint(fields=('dataset', 'name'), name='execution_matrix_dataset_name_uniq'),
        ]


class ExecutionPluginManifest(models.Model):
    """Persisted declaration for an enabled/disabled plugin capability."""

    KIND_CHOICES = (
        ('executor', 'Executor'),
        ('reporter', 'Reporter'),
        ('keyword', 'Keyword'),
        ('notifier', 'Notifier'),
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True, blank=True, related_name='execution_plugins')
    name = models.CharField(max_length=160)
    version = models.CharField(max_length=64)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    capabilities = models.JSONField(default=list, blank=True)
    schema = models.JSONField(default=dict, blank=True)
    permissions = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_plugin_manifests'
        constraints = [
            models.UniqueConstraint(fields=('project', 'name', 'version'), name='execution_plugin_project_version_uniq'),
        ]


class ExecutionProfile(models.Model):

    """Project-scoped CI execution trigger and quality-gate profile."""

    PROVIDER_CHOICES = (
        ('github', 'GitHub'),
        ('gitlab', 'GitLab'),
        ('jenkins', 'Jenkins'),
        ('generic', 'Generic'),
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='execution_profiles')
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True, default='')
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES, default='generic')
    source_type = models.CharField(max_length=32, choices=EXECUTION_SOURCE_CHOICES, default='manual')
    version = models.ForeignKey(Version, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_profiles')
    webhook_secret_ref = models.CharField(max_length=255, blank=True, default='', help_text='Environment variable name; secret value is never stored.')
    enabled = models.BooleanField(default=True)
    webhook_enabled = models.BooleanField(default=False)
    test_selection = models.JSONField(default=dict, blank=True)
    environment_snapshot = models.JSONField(default=dict, blank=True)
    retry_limit = models.PositiveIntegerField(default=0)
    retry_policy = models.JSONField(default=dict, blank=True)
    quality_gate = models.JSONField(default=dict, blank=True)
    callback_enabled = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_profiles_created')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_profiles'
        ordering = ['project_id', 'name']
        constraints = [
            models.UniqueConstraint(fields=('project', 'name'), name='execution_profile_project_name_uniq'),
        ]

    def __str__(self):
        return f'{self.project_id}:{self.name}'




NOTIFICATION_EVENT_CHOICES = (
    ('gate_failed', 'Release gate failed'),
    ('integration_failed', 'Integration health failed'),
    ('failure_reopened', 'Failure cluster repeated'),
    ('quarantine_expiring', 'Quarantine expiring'),
)


class ExecutionNotificationSubscription(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='execution_notification_subscriptions')
    channel = models.ForeignKey('monitor.NotificationChannel', on_delete=models.CASCADE, related_name='execution_subscriptions')
    event_type = models.CharField(max_length=32, choices=NOTIFICATION_EVENT_CHOICES)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_notification_subscriptions_created')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_notification_subscriptions'
        constraints = [
            models.UniqueConstraint(fields=('project', 'channel', 'event_type'), name='execution_notification_subscription_uniq'),
        ]
        indexes = [
            models.Index(fields=('project', 'event_type', 'enabled'), name='exec_notify_sub_proj_idx'),
        ]


class ExecutionNotificationEvent(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='execution_notification_events')
    subscription = models.ForeignKey(ExecutionNotificationSubscription, on_delete=models.CASCADE, related_name='events')
    event_key = models.CharField(max_length=255)
    event_type = models.CharField(max_length=32, choices=NOTIFICATION_EVENT_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='pending')
    payload = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default='')
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'execution_notification_events'
        constraints = [
            models.UniqueConstraint(fields=('subscription', 'event_key'), name='execution_notification_event_uniq'),
        ]
        indexes = [
            models.Index(fields=('project', 'event_type', '-created_at'), name='execution_notify_event_idx'),
        ]


class ExecutionRetryPolicy(models.Model):
    """Project/profile/case retry policy resolved before a new attempt."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='execution_retry_policies')
    profile = models.ForeignKey(
        ExecutionProfile,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='retry_policies',
    )
    case_key = models.CharField(max_length=255, blank=True, default='')
    name = models.CharField(max_length=160, blank=True, default='')
    enabled = models.BooleanField(default=True)
    max_attempts = models.PositiveIntegerField(default=1)
    retryable_error_types = models.JSONField(default=list, blank=True)
    retryable_statuses = models.JSONField(default=list, blank=True)
    backoff_seconds = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_retry_policies_created',
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_retry_policies'
        ordering = ['project_id', 'profile_id', 'case_key', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=('project', 'profile', 'case_key'),
                name='execution_retry_policy_scope_uniq',
            ),
            models.CheckConstraint(
                check=Q(max_attempts__gte=1),
                name='execution_retry_policy_attempts_positive',
            ),
        ]
        indexes = [
            models.Index(fields=('project', 'enabled'), name='exec_retry_proj_idx'),
            models.Index(fields=('profile', 'case_key'), name='exec_retry_case_idx'),
        ]

    def __str__(self):
        scope = self.case_key or 'project'
        if self.profile_id:
            scope = f'{self.profile_id}:{scope}'
        return f'{self.project_id}:{scope}'


class ExecutionQualityGateEvaluation(models.Model):
    """Immutable audit record for every quality-gate decision."""

    STAGE_CHOICES = (
        ('dispatch', 'Dispatch'),
        ('result', 'Result'),
        ('release', 'Release'),
        ('webhook', 'Webhook'),
    )

    job = models.ForeignKey(ExecutionJob, on_delete=models.CASCADE, related_name='quality_gate_evaluations')
    profile = models.ForeignKey(
        ExecutionProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quality_gate_evaluations',
    )
    attempt = models.ForeignKey(
        ExecutionAttempt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quality_gate_evaluations',
    )
    evaluation_key = models.CharField(max_length=64, default=_new_evaluation_key, null=True, blank=True)
    stage = models.CharField(max_length=16, choices=STAGE_CHOICES)
    passed = models.BooleanField(default=False)
    summary = models.JSONField(default=dict, blank=True)
    failures = models.JSONField(default=list, blank=True)
    policy_version = models.CharField(max_length=128, blank=True, default='')
    policy_snapshot = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=255, blank=True, default='')
    evaluated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_quality_gate_evaluations',
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'execution_quality_gate_evaluations'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=('evaluation_key',), name='execution_gate_evaluation_key_uniq'),
        ]
        indexes = [
            models.Index(fields=('job', 'stage', '-created_at'), name='execution_gate_job_stage_idx'),
            models.Index(fields=('passed', '-created_at'), name='execution_gate_passed_idx'),
        ]


class ExecutionFailureReview(models.Model):
    """Human classification feedback for one unified execution failure cluster."""

    CATEGORY_CHOICES = (
        ('PRODUCT', 'Product'),
        ('TEST', 'Test'),
        ('ENVIRONMENT', 'Environment'),
        ('INFRASTRUCTURE', 'Infrastructure'),
        ('DATA', 'Data'),
        ('PERFORMANCE', 'Performance'),
        ('TIMEOUT', 'Timeout'),
        ('UNKNOWN', 'Unknown'),
    )

    job = models.ForeignKey(ExecutionJob, on_delete=models.CASCADE, related_name='failure_reviews')
    case_key = models.CharField(max_length=255)
    signature = models.CharField(max_length=128)
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES)
    note = models.TextField(blank=True, default='')
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='execution_failure_reviews')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    history = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_failure_reviews'
        constraints = [
            models.UniqueConstraint(fields=('job', 'case_key', 'signature'), name='execution_failure_review_key_uniq'),
        ]
        indexes = [models.Index(fields=('job', 'category'), name='exec_failure_job_cat_idx')]


class ExecutionAISuggestion(models.Model):
    """Redacted, deterministic AI suggestion record requiring human review."""

    STATUS_CHOICES = (
        ('proposed', 'Proposed'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('expired', 'Expired'),
    )
    KIND_CHOICES = (
        ('failure_classification', 'Failure classification'),
        ('flaky_analysis', 'Flaky analysis'),
        ('retry_policy', 'Retry policy'),
        ('quarantine', 'Quarantine'),
        ('quality_gate', 'Quality gate'),
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='execution_ai_suggestions')
    job = models.ForeignKey(
        ExecutionJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ai_suggestions',
    )
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='proposed')
    target_type = models.CharField(max_length=64, blank=True, default='')
    target_key = models.CharField(max_length=255, blank=True, default='')
    source_fingerprint = models.CharField(max_length=128, blank=True, default='')
    prompt_version = models.CharField(max_length=64, default='1')
    model_provider = models.CharField(max_length=64, blank=True, default='')
    model_name = models.CharField(max_length=128, blank=True, default='')
    input_redacted = models.JSONField(default=dict, blank=True)
    output_redacted = models.JSONField(default=dict, blank=True)
    suggestion_hash = models.CharField(max_length=64, unique=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_micros = models.PositiveBigIntegerField(default=0)
    requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_ai_suggestions_requested',
    )
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_ai_suggestions_reviewed',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'execution_ai_suggestions'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=('project', 'status', '-created_at'), name='exec_ai_proj_status_idx'),
            models.Index(fields=('job', 'kind', '-created_at'), name='execution_ai_job_kind_idx'),
        ]


class ExecutionAISuggestionAudit(models.Model):
    """Append-only review history for an AI suggestion."""

    suggestion = models.ForeignKey(ExecutionAISuggestion, on_delete=models.CASCADE, related_name='audit_entries')
    action = models.CharField(max_length=32)
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='execution_ai_suggestion_audits',
    )
    previous_status = models.CharField(max_length=16, blank=True, default='')
    new_status = models.CharField(max_length=16, blank=True, default='')
    note = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'execution_ai_suggestion_audits'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=('suggestion', '-created_at'), name='exec_ai_audit_sugg_idx'),
        ]


class WebhookEvent(models.Model):
    """Deduplicated, auditable inbound CI webhook envelope."""

    STATUS_CHOICES = (
        ('received', 'Received'),
        ('processed', 'Processed'),
        ('rejected', 'Rejected'),
        ('failed', 'Failed'),
    )

    profile = models.ForeignKey(ExecutionProfile, on_delete=models.CASCADE, related_name='webhook_events')
    provider = models.CharField(max_length=20)
    external_event_id = models.CharField(max_length=255)
    payload_hash = models.CharField(max_length=128)
    payload_summary = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='received')
    job = models.ForeignKey(ExecutionJob, on_delete=models.SET_NULL, null=True, blank=True, related_name='webhook_events')
    error_message = models.TextField(blank=True, default='')
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'execution_webhook_events'
        ordering = ['-received_at']
        constraints = [
            models.UniqueConstraint(fields=('profile', 'external_event_id'), name='execution_webhook_profile_event_uniq'),
        ]

# Short aliases make the domain names convenient for adapter code without
# changing the explicit model names used in migrations and the API.
Attempt = ExecutionAttempt
StepResult = ExecutionStepResult
Artifact = ExecutionArtifact


__all__ = [
    'Artifact',
    'Attempt',
    'ExecutionAISuggestion',
    'ExecutionAISuggestionAudit',
    'ExecutionArtifact',
    'ExecutionAttempt',
    'ExecutionBuild',
    'ExecutionDataMatrix',
    'ExecutionDataset',
    'ExecutionDefectLink',
    'ExecutionEvent',
    'ExecutionFixture',
    'ExecutionJob',
    'ExecutionPluginManifest',
    'ExecutionProfile',
    'ExecutionQualityGateEvaluation',
    'ExecutionRequirementLink',
    'ExecutionRetryPolicy',
    'ExecutionRetryRequest',
    'ExecutionStepResult',
    'IdempotencyConflict',
    'RequirementTestCaseLink',
    'StepResult',
    'TestCaseDefectLink',
    'TestCaseRevision',
    'TestPlan',
    'TestRun',
    'TestRunCase',
    'TestRunCaseHistory',
]
