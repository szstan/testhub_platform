"""Celery worker for resumable, fenced LLM judge batches."""
import logging
import threading
import uuid
from datetime import timedelta

from celery import shared_task
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from .models import BATCH_WORKER_LEASE_SECONDS, JudgeBatch, JudgeBatchCaseResult, JudgeRecord

logger = logging.getLogger(__name__)

WORKER_HEARTBEAT_INTERVAL_SECONDS = max(1, BATCH_WORKER_LEASE_SECONDS // 3)
PUBLISHER_LEASE_SECONDS = BATCH_WORKER_LEASE_SECONDS
MAX_WORKER_RECOVERY_ATTEMPTS = 3


def worker_lease_deadline(now=None):
    return (now or timezone.now()) + timedelta(seconds=BATCH_WORKER_LEASE_SECONDS)


def _is_worker_owner(batch, request_id, attempt_token, now=None):
    now = now or timezone.now()
    return (
        batch.celery_task_id == request_id
        and batch.worker_attempt_token == attempt_token
        and batch.status == 'running'
        and not batch.is_paused
        and batch.worker_heartbeat_at is not None
        and batch.worker_lease_expires_at is not None
        and batch.worker_lease_expires_at > now
    )


def _is_paused_worker_owner(batch, attempt_token, now=None):
    now = now or timezone.now()
    return (
        batch.status == 'paused'
        and batch.is_paused
        and not batch.celery_task_id
        and batch.worker_attempt_token == attempt_token
        and batch.worker_heartbeat_at is not None
        and batch.worker_lease_expires_at is not None
        and batch.worker_lease_expires_at > now
    )


def _renew_worker_lease(batch_id, request_id, attempt_token, now=None):
    now = now or timezone.now()
    return JudgeBatch.objects.filter(
        pk=batch_id,
        worker_attempt_token=attempt_token,
        worker_lease_expires_at__gt=now,
        worker_heartbeat_at__isnull=False,
    ).filter(
        Q(status='running', is_paused=False, celery_task_id=request_id)
        | Q(status='paused', is_paused=True, celery_task_id='')
    ).update(
        worker_heartbeat_at=now,
        worker_lease_expires_at=worker_lease_deadline(now),
    ) == 1


class _WorkerLeaseHeartbeat:
    def __init__(self, batch_id, request_id, attempt_token):
        self.batch_id = batch_id
        self.request_id = request_id
        self.attempt_token = attempt_token
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f'judge-lease-{batch_id}',
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)

    def _run(self):
        try:
            while not self.stop_event.wait(WORKER_HEARTBEAT_INTERVAL_SECONDS):
                close_old_connections()
                try:
                    _renew_worker_lease(
                        self.batch_id, self.request_id, self.attempt_token,
                    )
                except Exception:
                    logger.exception('[JudgeBatch#%s] worker lease heartbeat failed', self.batch_id)
                finally:
                    close_old_connections()
        finally:
            close_old_connections()


def _mark_dispatch_acknowledged(batch_id, task_id, *, attempt_token='', published_task_id=None):
    updates = {'worker_lease_expires_at': None}
    if published_task_id:
        updates['celery_task_id'] = published_task_id
    return JudgeBatch.objects.filter(
        pk=batch_id,
        celery_task_id=task_id,
        worker_attempt_token=attempt_token,
        worker_heartbeat_at__isnull=True,
        status='running',
        is_paused=False,
    ).update(**updates) == 1


def _seed_legacy_results(batch):
    """Convert a legacy JSON checkpoint once, without rewriting it per case."""
    if batch.case_results.exists():
        return

    total = len(batch.cases_data or [])
    scored = min(max(int(batch.scored or 0), 0), total)
    if scored == 0:
        return

    legacy = batch.results_buffer or []
    rows = []
    for index in range(scored):
        result = legacy[index] if index < len(legacy) else None
        if not isinstance(result, dict):
            result = {'error': 'resume_missing_result', 'case_index': index}
        rows.append(JudgeBatchCaseResult(
            batch=batch,
            case_index=index,
            result=result,
        ))
    JudgeBatchCaseResult.objects.bulk_create(rows, ignore_conflicts=True)


def _normalize_checkpoint(batch):
    rows = list(batch.case_results.values_list('case_index', 'result'))
    indexes = {index for index, _ in rows}
    scored = 0
    while scored in indexes:
        scored += 1
    errors = sum(
        1 for index, result in rows
        if index < scored and isinstance(result, dict) and 'error' in result
    )
    progress = int(scored / max(1, len(batch.cases_data or [])) * 100)
    if (batch.scored, batch.error_count, batch.progress) != (scored, errors, progress):
        batch.scored = scored
        batch.error_count = errors
        batch.progress = progress
        batch.save(update_fields=['scored', 'error_count', 'progress'])
    return scored, errors


def _checkpoint_case(batch_id, request_id, attempt_token, case_index, total, result, record_fields=None):
    """Atomically persist the audit record, case result and progress under the worker fence."""
    with transaction.atomic():
        batch = JudgeBatch.objects.select_for_update().get(pk=batch_id)
        if not _is_worker_owner(batch, request_id, attempt_token):
            return 'fenced', None

        existing = JudgeBatchCaseResult.objects.select_related('record').filter(
            batch_id=batch_id, case_index=case_index,
        ).first()
        if existing:
            if case_index == batch.scored:
                batch.scored += 1
                if isinstance(existing.result, dict) and 'error' in existing.result:
                    batch.error_count += 1
                batch.progress = int(batch.scored / max(1, total) * 100)
                batch.worker_recovery_attempts = 0
                batch.save(update_fields=[
                    'scored', 'error_count', 'progress', 'worker_recovery_attempts',
                ])
            return 'existing', existing.record

        if case_index != batch.scored:
            raise RuntimeError(
                f'case checkpoint out of order: got {case_index}, expected {batch.scored}'
            )

        record = None
        if record_fields is not None:
            record = JudgeRecord.objects.create(**record_fields)
            result['record_id'] = record.id

        JudgeBatchCaseResult.objects.create(
            batch=batch,
            case_index=case_index,
            result=result,
            record=record,
        )
        batch.scored += 1
        if isinstance(result, dict) and 'error' in result:
            batch.error_count += 1
        batch.progress = int(batch.scored / max(1, total) * 100)
        batch.worker_recovery_attempts = 0
        batch.save(update_fields=[
            'scored', 'error_count', 'progress', 'worker_recovery_attempts',
        ])
        return 'saved', record


def _case_results(batch_id):
    return list(
        JudgeBatchCaseResult.objects.filter(batch_id=batch_id)
        .order_by('case_index')
        .values_list('result', flat=True)
    )


def _summary_or_fallback(service, results, batch_id, phase):
    try:
        return service.summarize_batch(results)
    except Exception as exc:
        logger.warning('[JudgeBatch#%s] %s summarize failed: %s', batch_id, phase, exc)
        return {
            'mean_score': 0, 'std_dev': 0, 'safety_pass_rate': 0,
            'critical_success_rate': 0, 'gate_zone': 'red', 'blocked': True,
        }


def _return_paused(self, batch_id, attempt_token, total, summary_service, summary=None):
    batch = JudgeBatch.objects.get(pk=batch_id)
    if not _is_paused_worker_owner(batch, attempt_token):
        return _fenced_result(batch_id, total)

    if summary is None:
        results = _case_results(batch_id)
        summary = _summary_or_fallback(summary_service, results, batch_id, 'paused')
    with transaction.atomic():
        batch = JudgeBatch.objects.select_for_update().get(pk=batch_id)
        owns_paused_claim = _is_paused_worker_owner(batch, attempt_token)
        if owns_paused_claim:
            batch.mean_score = summary['mean_score']
            batch.std_dev = summary['std_dev']
            batch.safety_pass_rate = summary['safety_pass_rate']
            batch.critical_success_rate = summary['critical_success_rate']
            batch.gate_zone = summary['gate_zone']
            batch.blocked = bool(summary.get('blocked', False))
            batch.worker_attempt_token = ''
            batch.worker_heartbeat_at = None
            batch.worker_lease_expires_at = None
            batch.save(update_fields=[
                'mean_score', 'std_dev', 'safety_pass_rate',
                'critical_success_rate', 'gate_zone', 'blocked',
                'worker_attempt_token', 'worker_heartbeat_at', 'worker_lease_expires_at',
            ])
        status = batch.status
        scored = batch.scored
        error_count = batch.error_count

    if not owns_paused_claim:
        return _fenced_result(batch_id, total)

    try:
        self.update_state(state='PAUSED', meta={
            'current': scored,
            'total': total,
            'progress': int(scored / max(1, total) * 100),
        })
    except Exception:
        pass
    return {
        'batch_id': batch_id,
        'status': status,
        'scored': scored,
        'total': total,
        'mean_score': summary['mean_score'],
        'error_cnt': error_count,
    }


def _paused_or_fenced(self, batch_id, attempt_token, total, summary_service, summary=None):
    batch = JudgeBatch.objects.get(pk=batch_id)
    if _is_paused_worker_owner(batch, attempt_token):
        return _return_paused(
            self, batch_id, attempt_token, total, summary_service, summary=summary,
        )
    return _fenced_result(batch_id, total)


def _fenced_result(batch_id, total):
    batch = JudgeBatch.objects.get(pk=batch_id)
    status = 'paused' if batch.is_paused or batch.status == 'paused' else 'superseded'
    return {'batch_id': batch_id, 'status': status, 'scored': batch.scored, 'total': total}


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True)
def score_batch_task(self, batch_id: int, claim_token=None):
    """Score each batch case once, with atomic per-case durable checkpoints."""
    try:
        request_id = self.request.id or f'sync-{batch_id}'
    except Exception:
        request_id = f'sync-{batch_id}'

    attempt_token = claim_token or uuid.uuid4().hex
    batch = None
    heartbeat = None
    try:
        with transaction.atomic():
            batch = JudgeBatch.objects.select_for_update().get(pk=batch_id)
            total = len(batch.cases_data or [])
            if batch.status in ('completed', 'failed', 'partial'):
                return {
                    'batch_id': batch.id,
                    'status': batch.status,
                    'scored': batch.scored,
                    'total': total,
                }
            if batch.is_paused or batch.status == 'paused':
                return {'batch_id': batch.id, 'status': 'paused', 'scored': batch.scored, 'total': total}

            now = timezone.now()
            if claim_token:
                if batch.worker_attempt_token != claim_token or batch.celery_task_id != request_id:
                    return {'batch_id': batch.id, 'status': 'superseded', 'scored': batch.scored, 'total': total}
            elif batch.celery_task_id and batch.celery_task_id != request_id and batch.status == 'running':
                logger.warning('[JudgeBatch#%s] stale worker delivery rejected: %s', batch_id, request_id)
                return {'batch_id': batch.id, 'status': 'superseded', 'scored': batch.scored, 'total': total}
            elif batch.worker_attempt_token:
                logger.warning('[JudgeBatch#%s] active worker attempt rejected: %s', batch_id, request_id)
                return {'batch_id': batch.id, 'status': 'already_running', 'scored': batch.scored, 'total': total}

            if batch.worker_heartbeat_at is not None:
                lease_is_live = (
                    batch.worker_lease_expires_at is not None
                    and batch.worker_lease_expires_at > now
                )
                result_status = 'already_running' if lease_is_live else 'lease_expired'
                return {
                    'batch_id': batch.id,
                    'status': result_status,
                    'scored': batch.scored,
                    'total': total,
                }

            started_at = batch.started_at or now
            claim_filter = JudgeBatch.objects.filter(
                pk=batch.pk,
                status=batch.status,
                is_paused=False,
                celery_task_id=batch.celery_task_id,
                worker_attempt_token=claim_token or '',
                worker_heartbeat_at__isnull=True,
            )
            claimed = claim_filter.update(
                total=total,
                status='running',
                celery_task_id=request_id,
                worker_attempt_token=attempt_token,
                worker_heartbeat_at=now,
                worker_lease_expires_at=worker_lease_deadline(now),
                error_message='',
                started_at=started_at,
            )
            if claimed != 1:
                batch.refresh_from_db()
                if batch.is_paused or batch.status == 'paused':
                    result_status = 'paused'
                elif batch.status == 'completed':
                    result_status = 'completed'
                elif batch.worker_attempt_token:
                    result_status = 'already_running'
                else:
                    result_status = 'superseded'
                return {
                    'batch_id': batch.id,
                    'status': result_status,
                    'scored': batch.scored,
                    'total': total,
                }

            batch.refresh_from_db()
            _seed_legacy_results(batch)
            start_index, _ = _normalize_checkpoint(batch)

        heartbeat = _WorkerLeaseHeartbeat(batch_id, request_id, attempt_token)
        heartbeat.start()
        from .service import JudgeService
        service = JudgeService()
        summary_service = JudgeService(rubric_id=batch.rubric_id)

        for case_index in range(start_index, total):
            batch.refresh_from_db(fields=[
                'is_paused', 'celery_task_id', 'worker_attempt_token',
                'worker_lease_expires_at', 'status', 'scored', 'progress',
            ])
            if batch.is_paused or batch.status == 'paused':
                return _return_paused(self, batch_id, attempt_token, total, summary_service)
            if not _is_worker_owner(batch, request_id, attempt_token):
                return _fenced_result(batch_id, total)

            existing = JudgeBatchCaseResult.objects.filter(
                batch_id=batch_id, case_index=case_index,
            ).first()
            if existing:
                state, _ = _checkpoint_case(
                    batch_id, request_id, attempt_token, case_index, total, existing.result,
                )
                if state == 'fenced':
                    return _paused_or_fenced(self, batch_id, attempt_token, total, summary_service)
            else:
                case = batch.cases_data[case_index]
                case_data = dict(case)
                case_data['rubric'] = case.get('rubric', batch.rubric_id)
                callback_state = {}

                def checkpoint_callback(record_fields, response):
                    state, record = _checkpoint_case(
                        batch_id,
                        request_id,
                        attempt_token,
                        case_index,
                        total,
                        response,
                        record_fields=record_fields,
                    )
                    callback_state['state'] = state
                    return record

                try:
                    service.score_single(
                        case_data,
                        batch=batch,
                        created_by=batch.created_by,
                        request_id=f'jb-{batch_id}-case-{case_index}',
                        checkpoint_callback=checkpoint_callback,
                    )
                    if callback_state.get('state') == 'fenced':
                        return _paused_or_fenced(self, batch_id, attempt_token, total, summary_service)
                except Exception as exc:
                    if callback_state.get('state') == 'fenced':
                        return _paused_or_fenced(self, batch_id, attempt_token, total, summary_service)
                    logger.warning('[JudgeBatch#%s] case %s scoring failed: %s', batch_id, case_index + 1, exc)
                    state, _ = _checkpoint_case(
                        batch_id,
                        request_id,
                        attempt_token,
                        case_index,
                        total,
                        {'error': str(exc), 'case_index': case_index},
                    )
                    if state == 'fenced':
                        return _paused_or_fenced(self, batch_id, attempt_token, total, summary_service)

            batch.refresh_from_db(fields=['scored', 'progress', 'error_count'])
            try:
                self.update_state(state='PROGRESS', meta={
                    'current': batch.scored,
                    'total': total,
                    'progress': batch.progress,
                })
            except Exception:
                pass

        results = _case_results(batch_id)
        summary = _summary_or_fallback(summary_service, results, batch_id, 'final')
        with transaction.atomic():
            batch = JudgeBatch.objects.select_for_update().get(pk=batch_id)
            if not _is_worker_owner(batch, request_id, attempt_token):
                return _paused_or_fenced(
                    self, batch_id, attempt_token, total, summary_service, summary=summary,
                )

            error_count = batch.error_count
            if total == 0:
                batch.status = 'failed'
                batch.error_message = '0 条用例'
            elif error_count == 0:
                batch.status = 'completed'
                batch.error_message = ''
            elif error_count >= total:
                batch.status = 'failed'
                batch.error_message = f'全部 {total} 条失败'
            else:
                batch.status = 'partial'
                batch.error_message = f'{error_count}/{total} 条失败'
            batch.mean_score = summary['mean_score']
            batch.std_dev = summary['std_dev']
            batch.safety_pass_rate = summary['safety_pass_rate']
            batch.critical_success_rate = summary['critical_success_rate']
            batch.gate_zone = summary['gate_zone']
            batch.blocked = bool(summary.get('blocked', False))
            batch.is_paused = False
            batch.worker_attempt_token = ''
            batch.worker_heartbeat_at = None
            batch.worker_lease_expires_at = None
            batch.completed_at = timezone.now()
            batch.save(update_fields=[
                'status', 'error_message', 'mean_score', 'std_dev',
                'safety_pass_rate', 'critical_success_rate', 'gate_zone',
                'blocked', 'is_paused', 'worker_attempt_token',
                'worker_heartbeat_at', 'worker_lease_expires_at', 'completed_at',
            ])

        return {
            'batch_id': batch.id,
            'status': batch.status,
            'scored': batch.scored,
            'total': batch.total,
            'mean_score': batch.mean_score,
            'error_cnt': batch.error_count,
        }
    except Exception as exc:
        logger.exception('[JudgeBatch#%s] batch scoring failed', batch_id)
        if batch is not None:
            JudgeBatch.objects.filter(
                pk=batch_id,
                celery_task_id=request_id,
                worker_attempt_token=attempt_token,
                worker_heartbeat_at__isnull=False,
                worker_lease_expires_at__gt=timezone.now(),
                status='running',
                is_paused=False,
            ).update(
                status='failed',
                error_message=str(exc),
                completed_at=timezone.now(),
                worker_attempt_token='',
                worker_heartbeat_at=None,
                worker_lease_expires_at=None,
            )
        raise
    finally:
        if heartbeat is not None:
            heartbeat.stop()


def reconcile_stale_batches(limit=10, now=None):
    """Requeue expired batch workers from durable checkpoints, with bounded retries.

    The returned count is the number of replacement tasks accepted by the broker.
    A committed publisher lease fences duplicate scheduler runs while dispatch is
    in flight; recovery attempts reset only after a case checkpoint is committed.
    """
    try:
        limit = min(100, max(0, int(limit)))
    except (TypeError, ValueError):
        return 0
    if limit == 0:
        return 0

    supplied_now = now is not None
    scan_now = now or timezone.now()
    stale_lease = (
        Q(worker_lease_expires_at__lte=scan_now)
        | Q(worker_lease_expires_at__isnull=True, worker_heartbeat_at__isnull=False)
    )
    stale_ids = list(
        JudgeBatch.objects.filter(status='running', is_paused=False)
        .filter(stale_lease)
        .order_by('worker_lease_expires_at', 'pk')
        .values_list('pk', flat=True)[:limit]
    )
    recovered = 0

    for batch_id in stale_ids:
        claim_now = scan_now if supplied_now else timezone.now()
        next_task_id = f'judge-recovery-{uuid.uuid4().hex}'
        next_token = uuid.uuid4().hex
        with transaction.atomic():
            batch = JudgeBatch.objects.select_for_update().filter(pk=batch_id).first()
            if (
                batch is None
                or batch.status != 'running'
                or batch.is_paused
                or (
                    batch.worker_lease_expires_at is not None
                    and batch.worker_lease_expires_at > claim_now
                )
            ):
                continue

            stale_filter = (
                Q(worker_lease_expires_at__lte=claim_now)
                | Q(worker_lease_expires_at__isnull=True, worker_heartbeat_at__isnull=False)
            )
            batch_filter = JudgeBatch.objects.filter(
                pk=batch.pk,
                status='running',
                is_paused=False,
                celery_task_id=batch.celery_task_id,
                worker_attempt_token=batch.worker_attempt_token,
            ).filter(stale_filter)

            if batch.worker_recovery_attempts >= MAX_WORKER_RECOVERY_ATTEMPTS:
                batch_filter.update(
                    status='failed',
                    error_message='worker lease expired after maximum recovery attempts',
                    completed_at=claim_now,
                    celery_task_id='',
                    worker_attempt_token='',
                    worker_heartbeat_at=None,
                    worker_lease_expires_at=None,
                )
                continue

            claimed = batch_filter.update(
                celery_task_id=next_task_id,
                worker_attempt_token=next_token,
                worker_heartbeat_at=None,
                worker_lease_expires_at=claim_now + timedelta(seconds=PUBLISHER_LEASE_SECONDS),
                worker_recovery_attempts=batch.worker_recovery_attempts + 1,
                error_message='worker lease expired; recovery task queued',
            )
            if claimed != 1:
                continue

        try:
            score_batch_task.apply_async(
                (batch_id, next_token),
                retry=False,
                ignore_result=False,
                task_id=next_task_id,
            )
            _mark_dispatch_acknowledged(
                batch_id,
                next_task_id,
                attempt_token=next_token,
            )
            recovered += 1
        except Exception as exc:
            logger.warning('[JudgeBatch#%s] stale worker recovery dispatch failed: %s', batch_id, exc)
            JudgeBatch.objects.filter(
                pk=batch_id,
                celery_task_id=next_task_id,
                worker_attempt_token=next_token,
                status='running',
            ).update(error_message=f'stale recovery dispatch failed: {exc}')
            break

    return recovered
