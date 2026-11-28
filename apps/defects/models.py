from django.db import models, transaction
from django.utils import timezone

from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.users.models import User
from apps.versions.models import Version


class DefectCodeSequence(models.Model):
    """Serialize daily defect number allocation across projects."""

    day = models.DateField(primary_key=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'defect_code_sequences'


class Defect(models.Model):
    """Bug defect model."""

    SEVERITY_CHOICES = [
        ('blocker', '阻塞'),
        ('critical', '严重'),
        ('major', '一般'),
        ('minor', '轻微'),
        ('suggestion', '建议'),
    ]

    PRIORITY_CHOICES = [
        ('p0', 'P0'),
        ('p1', 'P1'),
        ('p2', 'P2'),
        ('p3', 'P3'),
    ]

    STATUS_CHOICES = [
        ('new', '新建'),
        ('assigned', '已指派'),
        ('in_progress', '修复中'),
        ('resolved', '待回归'),
        ('verified', '回归通过'),
        ('rejected', '已驳回'),
        ('reopened', '重新打开'),
        ('closed', '已关闭'),
    ]

    TYPE_CHOICES = [
        ('functional', '功能缺陷'),
        ('ui', '界面缺陷'),
        ('compatibility', '兼容性缺陷'),
        ('performance', '性能缺陷'),
        ('security', '安全缺陷'),
        ('data', '数据缺陷'),
        ('other', '其他'),
    ]

    SOURCE_CHOICES = [
        ('manual', '手工测试'),
        ('api_testing', 'API测试'),
        ('ui_automation', 'UI自动化'),
        ('app_automation', 'APP自动化'),
        ('production', '生产反馈'),
    ]

    code = models.CharField(max_length=32, unique=True, blank=True, verbose_name='缺陷编号')
    title = models.CharField(max_length=300, verbose_name='缺陷标题')
    description = models.TextField(blank=True, verbose_name='缺陷描述')
    reproduce_steps = models.TextField(blank=True, verbose_name='复现步骤')
    expected_result = models.TextField(blank=True, verbose_name='预期结果')
    actual_result = models.TextField(blank=True, verbose_name='实际结果')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='defects', verbose_name='所属项目')
    version = models.ForeignKey(
        Version,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='defects',
        verbose_name='所属版本',
    )
    module = models.CharField(max_length=120, blank=True, verbose_name='所属模块')
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='major', verbose_name='严重级别')
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='p2', verbose_name='优先级')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='new', verbose_name='状态')
    defect_type = models.CharField(max_length=30, choices=TYPE_CHOICES, default='functional', verbose_name='缺陷类型')
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES, default='manual', verbose_name='来源')
    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reported_defects', verbose_name='提交人')
    assignee = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_defects',
        verbose_name='处理人',
    )
    verifier = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='verify_defects',
        verbose_name='验证人',
    )
    resolver = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='resolved_defects',
        verbose_name='修复人',
    )
    related_testcase = models.ForeignKey(
        TestCase,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='defects',
        verbose_name='关联用例',
    )
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name='解决时间')
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name='关闭时间')
    external_provider = models.CharField(max_length=32, blank=True, default='', verbose_name='外部平台')
    external_key = models.CharField(max_length=255, blank=True, default='', verbose_name='外部标识')
    external_url = models.URLField(blank=True, default='', verbose_name='外部链接')
    due_at = models.DateTimeField(null=True, blank=True, verbose_name='期望修复时间')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    def __str__(self):
        return f'{self.code} {self.title}' if self.code else self.title

    @transaction.atomic
    def save(self, *args, **kwargs):
        if not self.code:
            today = timezone.localdate()
            prefix = f'BUG-{today:%Y%m%d}-'
            DefectCodeSequence.objects.bulk_create(
                [DefectCodeSequence(day=today)], ignore_conflicts=True,
            )
            sequence = DefectCodeSequence.objects.select_for_update().get(day=today)

            next_number = sequence.last_number + 1
            if sequence.last_number == 0 or Defect.objects.filter(code=f'{prefix}{next_number:04d}').exists():
                # Seed legacy data once, or recover a manually inserted collision.
                # Normal allocation must not rescan every defect created today.
                for existing_code in Defect.objects.filter(code__startswith=prefix).values_list('code', flat=True).iterator():
                    try:
                        next_number = max(next_number, int(existing_code.rsplit('-', 1)[1]) + 1)
                    except (IndexError, ValueError):
                        continue
            sequence.last_number = next_number
            sequence.save(update_fields=['last_number'])
            self.code = f'{prefix}{sequence.last_number:04d}'
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = set(update_fields) | {'code'}
        super().save(*args, **kwargs)

    class Meta:
        db_table = 'defects'
        verbose_name = 'Bug缺陷'
        verbose_name_plural = 'Bug缺陷'
        ordering = ['-created_at']




class GitHubIssueIntegration(models.Model):
    """Project-scoped GitHub Issues integration using an environment token ref."""

    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name='github_issue_integration')
    repository_url = models.URLField(max_length=512)
    token_env_ref = models.CharField(max_length=255)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'github_issue_integrations'


class DefectTransition(models.Model):
    """Defect lifecycle transition history."""

    defect = models.ForeignKey(Defect, on_delete=models.CASCADE, related_name='transitions', verbose_name='缺陷')
    from_status = models.CharField(max_length=30, blank=True, verbose_name='原状态')
    to_status = models.CharField(max_length=30, verbose_name='目标状态')
    operator = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='defect_transitions',
        verbose_name='操作人',
    )
    target_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='defect_transition_targets',
        verbose_name='目标处理人',
    )
    comment = models.TextField(blank=True, verbose_name='流转说明')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='操作时间')

    class Meta:
        db_table = 'defect_transitions'
        verbose_name = '缺陷流转记录'
        verbose_name_plural = '缺陷流转记录'
        ordering = ['-created_at']


class DefectComment(models.Model):
    """Defect comment."""

    defect = models.ForeignKey(Defect, on_delete=models.CASCADE, related_name='comments', verbose_name='缺陷')
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='defect_comments', verbose_name='评论人')
    content = models.TextField(verbose_name='评论内容')
    created_at = models.DateTimeField(default=timezone.now, verbose_name='评论时间')

    class Meta:
        db_table = 'defect_comments'
        verbose_name = '缺陷评论'
        verbose_name_plural = '缺陷评论'
        ordering = ['-created_at']


class DefectAttachment(models.Model):
    """Defect attachment."""

    defect = models.ForeignKey(Defect, on_delete=models.CASCADE, related_name='attachments', verbose_name='缺陷')
    name = models.CharField(max_length=255, verbose_name='附件名称')
    file = models.FileField(upload_to='defect_attachments/', verbose_name='附件文件')
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='defect_attachments', verbose_name='上传人')
    uploaded_at = models.DateTimeField(default=timezone.now, verbose_name='上传时间')

    class Meta:
        db_table = 'defect_attachments'
        verbose_name = '缺陷附件'
        verbose_name_plural = '缺陷附件'
        ordering = ['-uploaded_at']
