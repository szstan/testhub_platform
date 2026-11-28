from celery import shared_task
from django.utils import timezone

from .models import TestCaseImportRecord
from .services import TestCaseExcelImportService
from .sync import MAX_SYNC_SECONDS


@shared_task(bind=True)
def import_testcases_from_excel(self, record_id: int):
    record = TestCaseImportRecord.objects.get(id=record_id)
    record.status = 'importing'
    record.progress = 5
    record.celery_task_id = self.request.id or ''
    record.error_message = ''
    record.save(update_fields=['status', 'progress', 'celery_task_id', 'error_message', 'updated_at'])

    try:
        summary = TestCaseExcelImportService.import_record(record)
        record.refresh_from_db()
        record.total_rows = summary.total_rows
        record.success_count = summary.success_count
        record.failed_count = summary.failed_count
        record.skip_count = summary.skip_count
        record.failure_details = summary.failure_details
        record.progress = 100
        record.completed_at = timezone.now()

        if summary.failed_count > 0 and summary.success_count > 0:
            record.status = 'partial_success'
        elif summary.failed_count > 0:
            record.status = 'failed'
        else:
            record.status = 'completed'

        failure_report = TestCaseExcelImportService.build_failure_report(record)
        if failure_report:
            record.failure_report_file.save(failure_report.name, failure_report, save=False)

        record.save()
        return {
            'record_id': record.id,
            'status': record.status,
            'success_count': record.success_count,
            'failed_count': record.failed_count,
        }
    except Exception as exc:
        record.status = 'failed'
        record.progress = 100
        record.error_message = str(exc)
        record.completed_at = timezone.now()
        record.save(update_fields=['status', 'progress', 'error_message', 'completed_at', 'updated_at'])
        raise


@shared_task(
    bind=True,
    ignore_result=True,
    soft_time_limit=MAX_SYNC_SECONDS + 10,
    time_limit=MAX_SYNC_SECONDS + 20,
)
def sync_testcase_profile(self, profile_id: int, actor_id: int):
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from apps.projects.access import user_can_write_project
    from .models import TestCaseSyncProfile
    from .sync import TestCaseSyncError, sync_profile

    task_id = self.request.id or ''
    if not task_id:
        return
    with transaction.atomic():
        profile = TestCaseSyncProfile.objects.select_for_update().select_related(
            'project', 'created_by',
        ).filter(pk=profile_id).first()
        if (
            profile is None or profile.sync_task_id != task_id
            or profile.sync_status != 'queued'
        ):
            return
        actor = get_user_model().objects.filter(pk=actor_id, is_active=True).first()
        if actor is None or not user_can_write_project(actor, profile.project):
            profile.sync_status = 'failed'
            profile.sync_error = '项目写入权限已撤销'
            profile.save(update_fields=['sync_status', 'sync_error', 'updated_at'])
            return
        profile.sync_status = 'running'
        profile.sync_error = ''
        profile.save(update_fields=['sync_status', 'sync_error', 'updated_at'])

    try:
        summary = sync_profile(profile, actor=actor, expected_task_id=task_id)
    except Exception as exc:
        TestCaseSyncProfile.objects.filter(
            pk=profile_id, sync_task_id=task_id, sync_status='running',
        ).update(
            sync_status='failed',
            sync_error=(
                str(exc) if isinstance(exc, TestCaseSyncError)
                else '同步失败，请检查配置或联系管理员'
            ),
            updated_at=timezone.now(),
        )
        raise

    result = {
        'profile_id': summary.profile_id,
        'provider': summary.provider,
        'commit': summary.commit,
        'files': summary.files,
        'created': summary.created,
        'updated': summary.updated,
        'skipped': summary.skipped,
        'conflicts': summary.conflicts,
        'warnings': summary.warnings,
    }
    TestCaseSyncProfile.objects.filter(
        pk=profile_id, sync_task_id=task_id, sync_status='running',
    ).update(
        sync_status='completed', sync_error='', sync_result=result,
        updated_at=timezone.now(),
    )
