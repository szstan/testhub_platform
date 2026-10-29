from django.conf import settings
from django.db import models

from apps.projects.models import Project


class QualitySourceType(models.TextChoices):
    API = 'API', 'API'
    UI = 'UI', 'UI'
    APP = 'APP', 'APP'
    PERF = 'PERF', 'Performance'


class QualitySourceScope(models.TextChoices):
    REQUEST = 'REQUEST', 'Request'
    CASE = 'CASE', 'Case'
    SUITE = 'SUITE', 'Suite'
    SCENARIO = 'SCENARIO', 'Scenario'


class QualityRunStatus(models.TextChoices):
    INGESTED = 'INGESTED', 'Ingested'
    PARTIAL = 'PARTIAL', 'Partial'
    FAILED = 'FAILED', 'Failed'
    SOURCE_MISSING = 'SOURCE_MISSING', 'Source missing'


class QualityItemOutcome(models.TextChoices):
    PASS = 'PASS', 'Pass'
    FAIL = 'FAIL', 'Fail'
    SKIP = 'SKIP', 'Skip'
    ERROR = 'ERROR', 'Error'
    INCONCLUSIVE = 'INCONCLUSIVE', 'Inconclusive'
    NOT_RUN = 'NOT_RUN', 'Not run'


class QualityEvidenceType(models.TextChoices):
    REQUEST = 'REQUEST', 'Request'
    RESPONSE = 'RESPONSE', 'Response'
    ASSERTION = 'ASSERTION', 'Assertion'
    LOG = 'LOG', 'Log'
    SCREENSHOT = 'SCREENSHOT', 'Screenshot'
    TRACE = 'TRACE', 'Trace'
    ALLURE = 'ALLURE', 'Allure'
    METRIC = 'METRIC', 'Metric'
    ARTIFACT = 'ARTIFACT', 'Artifact'


class QualityEvidenceAvailability(models.TextChoices):
    AVAILABLE = 'AVAILABLE', 'Available'
    UNAVAILABLE = 'UNAVAILABLE', 'Unavailable'
    REDACTED = 'REDACTED', 'Redacted'


class QualityFailureCategory(models.TextChoices):
    PRODUCT = 'PRODUCT', 'Product'
    TEST = 'TEST', 'Test'
    ENVIRONMENT = 'ENVIRONMENT', 'Environment'
    INFRASTRUCTURE = 'INFRASTRUCTURE', 'Infrastructure'
    DATA = 'DATA', 'Data'
    PERFORMANCE = 'PERFORMANCE', 'Performance'
    TIMEOUT = 'TIMEOUT', 'Timeout'
    UNKNOWN = 'UNKNOWN', 'Unknown'


class QualityFailureClassifier(models.TextChoices):
    RULE = 'RULE', 'Rule'
    HUMAN = 'HUMAN', 'Human'
    AI = 'AI', 'AI'


class QualityReliabilityStatus(models.TextChoices):
    INSUFFICIENT_DATA = 'INSUFFICIENT_DATA', 'Insufficient data'
    STABLE = 'STABLE', 'Stable'
    FLAKY = 'FLAKY', 'Flaky'


class QualityQuarantineStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', 'Active'
    RELEASED = 'RELEASED', 'Released'
    EXPIRED = 'EXPIRED', 'Expired'


class QualityQuarantineApprovalStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'


class QualityMetricAggregation(models.TextChoices):
    VALUE = 'VALUE', 'Value'
    AVG = 'AVG', 'Average'
    P50 = 'P50', 'P50'
    P95 = 'P95', 'P95'
    P99 = 'P99', 'P99'
    RATE = 'RATE', 'Rate'
    COUNT = 'COUNT', 'Count'


class QualityBaselineType(models.TextChoices):
    MANUAL = 'MANUAL', 'Manual'
    IMPORTED = 'IMPORTED', 'Imported'


class QualityComparisonVerdict(models.TextChoices):
    WITHIN_TOLERANCE = 'WITHIN_TOLERANCE', 'Within tolerance'
    REGRESSED = 'REGRESSED', 'Regressed'
    NO_BASELINE = 'NO_BASELINE', 'No baseline'
    NOT_COMPARABLE = 'NOT_COMPARABLE', 'Not comparable'
    ERROR = 'ERROR', 'Error'


class QualityRun(models.Model):
    source_type = models.CharField(max_length=16, choices=QualitySourceType.choices)
    source_scope = models.CharField(max_length=16, choices=QualitySourceScope.choices)
    source_execution_id = models.CharField(max_length=128)
    project_type = models.CharField(max_length=64)
    project_id = models.CharField(max_length=128)
    source_asset_id = models.CharField(max_length=128, blank=True, default='')
    source_asset_name = models.CharField(max_length=255, blank=True, default='')
    source_status = models.CharField(max_length=64, blank=True, default='')
    quality_status = models.CharField(
        max_length=32,
        choices=QualityRunStatus.choices,
        default=QualityRunStatus.INGESTED,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    environment_fingerprint = models.CharField(max_length=128, default='unknown')
    source_fingerprint = models.CharField(max_length=128, blank=True, default='')
    adapter_version = models.CharField(max_length=32, default='1.0')
    redaction_version = models.CharField(max_length=32, default='1.0')
    schema_version = models.PositiveSmallIntegerField(default=1)
    summary = models.JSONField(default=dict, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quality_runs'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['source_type', 'source_execution_id'],
                name='qi_run_source_exec_uq',
            ),
        ]
        indexes = [
            models.Index(
                fields=['source_type', 'project_type', 'project_id', '-created_at'],
                name='qi_run_source_proj_idx',
            ),
            models.Index(
                fields=['quality_status', '-created_at'],
                name='qi_run_status_created_idx',
            ),
        ]

    def __str__(self):
        return f'{self.source_type}:{self.source_execution_id}'


class QualityRunItem(models.Model):
    run = models.ForeignKey(QualityRun, on_delete=models.CASCADE, related_name='items')
    source_item_type = models.CharField(max_length=64)
    source_item_id = models.CharField(max_length=128, blank=True, default='')
    test_key = models.CharField(max_length=255)
    name = models.CharField(max_length=255, blank=True, default='')
    source_status = models.CharField(max_length=64, blank=True, default='')
    outcome = models.CharField(max_length=24, choices=QualityItemOutcome.choices)
    attempt = models.PositiveIntegerField(default=1)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    environment_fingerprint = models.CharField(max_length=128, default='unknown')
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quality_run_items'
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'test_key', 'attempt'],
                name='qi_item_run_key_attempt_uq',
            ),
        ]
        indexes = [
            models.Index(fields=['run', 'outcome'], name='qi_item_run_outcome_idx'),
            models.Index(fields=['test_key', '-created_at'], name='qi_item_key_created_idx'),
        ]

    def __str__(self):
        return self.name or self.test_key


class QualityEvidence(models.Model):
    run = models.ForeignKey(QualityRun, on_delete=models.CASCADE, related_name='evidence')
    item = models.ForeignKey(
        QualityRunItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='evidence',
    )
    evidence_type = models.CharField(max_length=24, choices=QualityEvidenceType.choices)
    snippet = models.TextField(blank=True, default='')
    source_uri = models.CharField(max_length=1024, blank=True, default='')
    source_field = models.CharField(max_length=255, blank=True, default='')
    sha256 = models.CharField(max_length=64, blank=True, default='')
    mime_type = models.CharField(max_length=128, blank=True, default='')
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    availability = models.CharField(
        max_length=16,
        choices=QualityEvidenceAvailability.choices,
        default=QualityEvidenceAvailability.AVAILABLE,
    )
    provenance = models.JSONField(default=dict, blank=True)
    redaction_version = models.CharField(max_length=32, default='1.0')
    captured_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'quality_evidence'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'evidence_type', 'sha256'],
                name='qi_evidence_item_type_hash_uq',
            ),
        ]
        indexes = [
            models.Index(fields=['run', 'evidence_type'], name='qi_evidence_run_type_idx'),
            models.Index(fields=['availability', 'mime_type', '-created_at'], name='qi_evidence_avail_mime_idx'),
        ]


class QualityFailure(models.Model):
    run = models.ForeignKey(QualityRun, on_delete=models.CASCADE, related_name='failures')
    item = models.ForeignKey(
        QualityRunItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='failures',
    )
    dedupe_key = models.CharField(max_length=512, unique=True)
    cluster_key = models.CharField(max_length=128, blank=True, default='', db_index=True)
    taxonomy_code = models.CharField(max_length=32, choices=QualityFailureCategory.choices)
    subtype = models.CharField(max_length=64, blank=True, default='')
    phase = models.CharField(max_length=64, blank=True, default='')
    signature = models.CharField(max_length=128)
    source_error_code = models.CharField(max_length=128, blank=True, default='')
    message = models.TextField(blank=True, default='')
    stack_excerpt = models.TextField(blank=True, default='')
    retryable = models.BooleanField(default=False)
    details = models.JSONField(default=dict, blank=True)
    initial_taxonomy_code = models.CharField(
        max_length=32,
        choices=QualityFailureCategory.choices,
    )
    classifier = models.CharField(
        max_length=16,
        choices=QualityFailureClassifier.choices,
        default=QualityFailureClassifier.RULE,
    )
    confidence = models.FloatField(default=1.0)
    rule_id = models.CharField(max_length=128, blank=True, default='')
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_quality_failures',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quality_failures'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['taxonomy_code', '-created_at'], name='qi_failure_tax_created_idx'),
            models.Index(fields=['signature', '-created_at'], name='qi_failure_sig_created_idx'),
            models.Index(fields=['cluster_key', '-created_at'], name='qi_failure_cluster_idx'),
        ]


class QualityFailureCluster(models.Model):
    """Cross-run deterministic failure cluster with human-owned remediation links."""

    cluster_key = models.CharField(max_length=128, unique=True)
    source_type = models.CharField(max_length=16)
    project_type = models.CharField(max_length=64)
    project_id = models.CharField(max_length=128)
    test_key = models.CharField(max_length=255, blank=True, default='')
    environment_fingerprint = models.CharField(max_length=128, default='unknown')
    signature = models.CharField(max_length=128)
    taxonomy_code = models.CharField(max_length=32, choices=QualityFailureCategory.choices, default=QualityFailureCategory.UNKNOWN)
    subtype = models.CharField(max_length=64, blank=True, default='')
    mapped_project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quality_failure_clusters_projected',
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='owned_quality_failure_clusters',
    )
    defect = models.ForeignKey(
        'defects.Defect',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quality_failure_clusters',
    )
    external_provider = models.CharField(max_length=32, blank=True, default='')
    external_key = models.CharField(max_length=255, blank=True, default='')
    external_url = models.URLField(blank=True, default='')
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    occurrence_count = models.PositiveIntegerField(default=0)
    last_failure_run = models.ForeignKey(
        QualityRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='last_failure_clusters',
    )
    last_fixed_run = models.ForeignKey(
        QualityRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='fixed_failure_clusters',
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quality_failure_clusters'
        ordering = ['-last_seen_at', '-created_at']
        indexes = [
            models.Index(fields=['source_type', 'project_type', 'project_id', '-last_seen_at'], name='qi_cluster_project_seen_idx'),
            models.Index(fields=['signature', '-last_seen_at'], name='qi_cluster_signature_seen_idx'),
            models.Index(fields=['owner', 'resolved_at'], name='qi_cluster_owner_resolved_idx'),
        ]


class QualityFailureClusterAudit(models.Model):
    """Append-only audit for owner and defect changes on a failure cluster."""

    cluster = models.ForeignKey(QualityFailureCluster, on_delete=models.CASCADE, related_name='audits')
    action = models.CharField(max_length=32)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quality_failure_cluster_audits',
    )
    previous_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'quality_failure_cluster_audits'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['cluster', '-created_at'], name='qi_cluster_audit_idx'),
        ]


class QualityFailureReviewAudit(models.Model):
    """Append-only human review history for a quality failure."""

    failure = models.ForeignKey(QualityFailure, on_delete=models.SET_NULL, null=True, related_name='review_audits')
    source_failure_key = models.CharField(max_length=512)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quality_failure_review_audits',
    )
    previous_taxonomy = models.CharField(max_length=32, blank=True, default='')
    new_taxonomy = models.CharField(max_length=32, blank=True, default='')
    note = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'quality_failure_review_audits'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['source_failure_key', '-created_at'], name='qi_failure_review_key_idx'),
            models.Index(fields=['failure', '-created_at'], name='qi_failure_review_idx'),
        ]


class QualityTestProfile(models.Model):
    source_type = models.CharField(max_length=16, choices=QualitySourceType.choices)
    project_type = models.CharField(max_length=64)
    project_id = models.CharField(max_length=128)
    test_key = models.CharField(max_length=255)
    name = models.CharField(max_length=255, blank=True, default='')
    environment_fingerprint = models.CharField(max_length=128, default='unknown')
    window_size = models.PositiveSmallIntegerField(default=20)
    min_attempts = models.PositiveSmallIntegerField(default=5)
    eligible_attempts = models.PositiveIntegerField(default=0)
    pass_count = models.PositiveIntegerField(default=0)
    fail_count = models.PositiveIntegerField(default=0)
    transition_count = models.PositiveIntegerField(default=0)
    flaky_score = models.FloatField(default=0.0)
    reliability_status = models.CharField(
        max_length=24,
        choices=QualityReliabilityStatus.choices,
        default=QualityReliabilityStatus.INSUFFICIENT_DATA,
    )
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quality_test_profiles'
        ordering = ['-last_seen_at', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=[
                    'source_type',
                    'project_type',
                    'project_id',
                    'test_key',
                    'environment_fingerprint',
                ],
                name='qi_profile_identity_uq',
            ),
        ]
        indexes = [
            models.Index(
                fields=['source_type', 'reliability_status', '-last_seen_at'],
                name='qi_profile_status_seen_idx',
            ),
        ]

    def __str__(self):
        return self.name or self.test_key


class QualityQuarantine(models.Model):
    profile = models.ForeignKey(
        QualityTestProfile,
        on_delete=models.CASCADE,
        related_name='quarantines',
    )
    status = models.CharField(
        max_length=16,
        choices=QualityQuarantineStatus.choices,
        default=QualityQuarantineStatus.ACTIVE,
    )
    reason = models.TextField()
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='owned_quality_quarantines',
    )
    approval_status = models.CharField(
        max_length=16,
        choices=QualityQuarantineApprovalStatus.choices,
        default=QualityQuarantineApprovalStatus.APPROVED,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_quality_quarantines',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_note = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_quality_quarantines',
    )
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='released_quality_quarantines',
    )
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quality_quarantines'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at'], name='qi_quarantine_status_idx'),
            models.Index(fields=['profile', 'status'], name='qi_quarantine_profile_idx'),
        ]


class QualityQuarantineAudit(models.Model):
    """Immutable audit trail for quarantine governance actions."""

    ACTION_CHOICES = (
        ('CREATE', 'Create'),
        ('APPROVE', 'Approve'),
        ('REJECT', 'Reject'),
        ('RELEASE', 'Release'),
        ('EXPIRE', 'Expire'),
        ('OVERRIDE', 'Override'),
    )

    quarantine = models.ForeignKey(QualityQuarantine, on_delete=models.CASCADE, related_name='audit_entries')
    profile = models.ForeignKey(QualityTestProfile, on_delete=models.CASCADE, related_name='quarantine_audits')
    execution_job = models.ForeignKey(
        'executions.ExecutionJob',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quarantine_audits',
    )
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quality_quarantine_audits',
    )
    reason = models.TextField(blank=True, default='')
    previous_status = models.CharField(max_length=16, blank=True, default='')
    new_status = models.CharField(max_length=16, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'quality_quarantine_audits'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['quarantine', '-created_at'], name='qi_quarantine_audit_idx'),
            models.Index(fields=['execution_job', '-created_at'], name='qi_quarantine_job_audit_idx'),
            models.Index(fields=['action', '-created_at'], name='qi_quarantine_action_idx'),
        ]


class QualityMetricObservation(models.Model):
    run = models.ForeignKey(QualityRun, on_delete=models.CASCADE, related_name='metrics')
    item = models.ForeignKey(
        QualityRunItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='metrics',
    )
    profile = models.ForeignKey(
        QualityTestProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='metric_observations',
    )
    metric_key = models.CharField(max_length=128)
    sample_key = models.CharField(max_length=255, blank=True, default='')
    value = models.FloatField()
    unit = models.CharField(max_length=32, blank=True, default='')
    aggregation = models.CharField(
        max_length=16,
        choices=QualityMetricAggregation.choices,
        default=QualityMetricAggregation.VALUE,
    )
    dimensions = models.JSONField(default=dict, blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'quality_metric_observations'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'metric_key', 'sample_key'],
                name='qi_metric_run_key_sample_uq',
            ),
        ]
        indexes = [
            models.Index(fields=['metric_key', '-created_at'], name='qi_metric_key_created_idx'),
            models.Index(fields=['profile', 'metric_key'], name='qi_metric_profile_key_idx'),
        ]


class QualityBaseline(models.Model):
    source_type = models.CharField(max_length=16, choices=QualitySourceType.choices)
    project_type = models.CharField(max_length=64)
    project_id = models.CharField(max_length=128)
    profile = models.ForeignKey(
        QualityTestProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='baselines',
    )
    metric_key = models.CharField(max_length=128)
    environment_fingerprint = models.CharField(max_length=128, default='unknown')
    baseline_type = models.CharField(
        max_length=16,
        choices=QualityBaselineType.choices,
        default=QualityBaselineType.MANUAL,
    )
    source_baseline_key = models.CharField(max_length=255, blank=True, default='')
    source_run = models.ForeignKey(
        QualityRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='baselines',
    )
    value = models.FloatField()
    unit = models.CharField(max_length=32, blank=True, default='')
    tolerance = models.FloatField(default=0.0)
    direction = models.CharField(max_length=24, default='LOWER_IS_BETTER')
    snapshot = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_quality_baselines',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_quality_baselines',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'quality_baselines'
        ordering = ['-updated_at', '-created_at']
        indexes = [
            models.Index(
                fields=['source_type', 'project_type', 'project_id', 'metric_key'],
                name='qi_baseline_metric_idx',
            ),
            models.Index(fields=['profile', 'metric_key', 'active'], name='qi_baseline_profile_idx'),
        ]


class QualityComparison(models.Model):
    run = models.ForeignKey(QualityRun, on_delete=models.CASCADE, related_name='comparisons')
    baseline = models.ForeignKey(
        QualityBaseline,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='comparisons',
    )
    profile = models.ForeignKey(
        QualityTestProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='comparisons',
    )
    metric_key = models.CharField(max_length=128)
    baseline_value = models.FloatField(null=True, blank=True)
    actual_value = models.FloatField(null=True, blank=True)
    delta = models.FloatField(null=True, blank=True)
    tolerance = models.FloatField(null=True, blank=True)
    unit = models.CharField(max_length=32, blank=True, default='')
    verdict = models.CharField(max_length=24, choices=QualityComparisonVerdict.choices)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'quality_comparisons'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['run', 'baseline', 'metric_key'],
                name='qi_compare_run_base_metric_uq',
            ),
        ]
        indexes = [
            models.Index(fields=['verdict', '-created_at'], name='qi_compare_verdict_idx'),
            models.Index(fields=['metric_key', '-created_at'], name='qi_compare_metric_idx'),
        ]
