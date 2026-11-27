"""Durable worker implementation for testcase generation."""

import asyncio
import json
import logging
import uuid

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from .ai_contracts import AIContractError, parse_generated_cases, validate_executable_cases
from .models import AIModelService, GenerationConfig, TestCaseGenerationTask

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ('generating', 'reviewing', 'revising')
MAX_PROVIDER_RETRIES = min(
    max(int(getattr(settings, 'REQUIREMENT_GENERATION_PROVIDER_RETRIES', 1)), 0), 1
)
WRITER_STAGE_TIMEOUT_SECONDS = min(
    max(int(getattr(settings, 'REQUIREMENT_GENERATION_WRITER_TIMEOUT_SECONDS', 900)), 1), 1800
)
MAX_REVIEW_STAGE_TIMEOUT_SECONDS = 900
PROVIDER_RETRY_BACKOFF_SECONDS = 0.25


class GenerationClaimLost(Exception):
    """Raised when cancellation or a newer worker supersedes this worker."""


class GenerationStageTimeout(TimeoutError):
    def __init__(self, stage, timeout_seconds):
        self.stage = stage
        self.timeout_seconds = timeout_seconds
        super().__init__(f'{stage} stage timed out after {timeout_seconds} seconds')


def _with_connection_cleanup(operation, *args, **kwargs):
    close_old_connections()
    try:
        return operation(*args, **kwargs)
    finally:
        close_old_connections()


def _update_claimed(task_pk, claim_id, execution_claim_token, **fields):
    fields['updated_at'] = timezone.now()
    rows = TestCaseGenerationTask.objects.filter(
        pk=task_pk,
        celery_task_id=claim_id,
        worker_claim_id=execution_claim_token,
        status__in=ACTIVE_STATUSES,
    ).update(**fields)
    if rows != 1:
        raise GenerationClaimLost(f'generation claim lost for task {task_pk}')


def _assert_claimed(task_pk, claim_id, execution_claim_token):
    if not TestCaseGenerationTask.objects.filter(
        pk=task_pk,
        celery_task_id=claim_id,
        worker_claim_id=execution_claim_token,
        status__in=ACTIVE_STATUSES,
    ).exists():
        raise GenerationClaimLost(f'generation claim lost for task {task_pk}')


async def _run_provider_stage(task_pk, claim_id, worker_claim_id, stage, timeout_seconds, call, reset):
    for attempt in range(MAX_PROVIDER_RETRIES + 1):
        await sync_to_async(_with_connection_cleanup, thread_sensitive=True)(
            _assert_claimed, task_pk, claim_id, worker_claim_id
        )
        try:
            result = await asyncio.wait_for(call(), timeout=timeout_seconds)
            await sync_to_async(_with_connection_cleanup, thread_sensitive=True)(
                _assert_claimed, task_pk, claim_id, worker_claim_id
            )
            return result
        except asyncio.TimeoutError as exc:
            raise GenerationStageTimeout(stage, timeout_seconds) from exc
        except GenerationClaimLost:
            raise
        except Exception:
            if attempt >= MAX_PROVIDER_RETRIES:
                raise
            logger.warning(
                'Retrying testcase generation provider stage: task_id=%s stage=%s attempt=%s',
                task_pk,
                stage,
                attempt + 1,
                exc_info=True,
            )
            await reset()
            await asyncio.sleep(PROVIDER_RETRY_BACKOFF_SECONDS * (attempt + 1))


def _finalize_cases(content, *, fix_incomplete=False):
    if fix_incomplete:
        content = AIModelService.fix_incomplete_last_case(content)
    return AIModelService.renumber_test_cases(AIModelService.sort_test_cases_by_id(content))


def _review_timeout(config):
    configured = config.review_timeout if config else 120
    return min(max(int(configured), 1), MAX_REVIEW_STAGE_TIMEOUT_SECONDS)


def execute_generation_task(task_pk, claim_id):
    """Claim and execute one persisted generation task in a Celery worker."""
    close_old_connections()
    loop = None
    current_stage = 'claim'
    try:
        worker_claim_id = uuid.uuid4().hex
        with transaction.atomic():
            task = TestCaseGenerationTask.objects.select_for_update().filter(pk=task_pk).first()
            can_claim = (
                task is not None
                and task.celery_task_id == claim_id
                and task.status in ('pending', *ACTIVE_STATUSES)
            )
            if not can_claim:
                logger.info('Generation task was cancelled or claimed elsewhere: task_id=%s', task_pk)
                return {'task_id': task_pk, 'status': 'not_claimed'}

            redelivered = task.status in ACTIVE_STATUSES
            task.status = 'generating'
            task.worker_claim_id = worker_claim_id
            task.progress = 10
            task.error_message = ''
            task.updated_at = timezone.now()
            update_fields = ['status', 'worker_claim_id', 'progress', 'error_message', 'updated_at']
            if redelivered:
                task.stream_buffer = ''
                task.stream_position = 0
                task.last_stream_update = None
                task.generated_test_cases = ''
                task.review_feedback = ''
                task.final_test_cases = ''
                task.generation_log = ''
                task.completed_at = None
                update_fields.extend([
                    'stream_buffer', 'stream_position', 'last_stream_update',
                    'generated_test_cases', 'review_feedback', 'final_test_cases',
                    'generation_log', 'completed_at',
                ])
                logger.warning('Reclaiming redelivered generation task: task_id=%s', task_pk)
            task.save(update_fields=update_fields)
            task = TestCaseGenerationTask.objects.select_related(
                'writer_prompt_config',
                'writer_model_config',
                'reviewer_prompt_config',
                'reviewer_model_config',
            ).get(pk=task_pk)

        generation_config = GenerationConfig.get_active_config()
        enable_auto_review = bool(
            task.reviewer_model_config_id and task.reviewer_prompt_config_id
        )
        review_timeout = _review_timeout(generation_config)
        logger.info(
            'Starting testcase generation: task_id=%s output_mode=%s writer_config=%s reviewer_config=%s',
            task.task_id,
            task.output_mode,
            task.writer_model_config_id,
            task.reviewer_model_config_id,
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def persist(**fields):
            _update_claimed(task_pk, claim_id, worker_claim_id, **fields)

        async def persist_async(**fields):
            return await sync_to_async(_with_connection_cleanup, thread_sensitive=True)(
                persist, **fields
            )

        async def persist_progress(**fields):
            try:
                await persist_async(**fields)
            except GenerationClaimLost:
                raise
            except Exception:
                logger.warning('Unable to persist streamed generation content: task_id=%s', task_pk, exc_info=True)

        if task.output_mode == 'stream':
            writer_buffer = ''
            persist(
                stream_buffer='',
                stream_position=0,
                last_stream_update=None,
                progress=30,
            )

            async def stream_callback(chunk):
                nonlocal writer_buffer
                writer_buffer += chunk
                position = len(writer_buffer)
                if position % 500 < 20 or len(chunk) > 100:
                    await persist_progress(
                        stream_buffer=writer_buffer,
                        stream_position=position,
                        last_stream_update=timezone.now(),
                    )

            async def reset_writer():
                nonlocal writer_buffer
                writer_buffer = ''
                await persist_async(
                    stream_buffer='',
                    stream_position=0,
                    last_stream_update=timezone.now(),
                )

            current_stage = 'writer'
            generated_cases = loop.run_until_complete(_run_provider_stage(
                task_pk,
                claim_id,
                worker_claim_id,
                current_stage,
                WRITER_STAGE_TIMEOUT_SECONDS,
                lambda: AIModelService.generate_test_cases_stream(task, callback=stream_callback),
                reset_writer,
            ))
            from .ai_contracts import summarize_generation_output

            persist(
                stream_buffer=str(generated_cases),
                stream_position=len(str(generated_cases)),
                last_stream_update=timezone.now(),
                generated_test_cases=str(generated_cases),
                generation_log=json.dumps(summarize_generation_output(generated_cases), ensure_ascii=False),
                progress=60,
            )
        else:
            persist(progress=30)
            current_stage = 'writer'
            generated_cases = loop.run_until_complete(_run_provider_stage(
                task_pk,
                claim_id,
                worker_claim_id,
                current_stage,
                WRITER_STAGE_TIMEOUT_SECONDS,
                lambda: AIModelService.generate_test_cases(task),
                _reset_noop,
            ))
            from .ai_contracts import summarize_generation_output

            persist(
                generated_test_cases=generated_cases,
                generation_log=json.dumps(summarize_generation_output(generated_cases), ensure_ascii=False),
                progress=60,
            )

        final_raw_output = getattr(generated_cases, 'raw_output', None) or str(generated_cases or '')
        final_cases = None
        if enable_auto_review and task.reviewer_model_config and task.reviewer_prompt_config:
            review_chunks = []
            persist(status='reviewing', progress=70)

            async def review_callback(chunk):
                review_chunks.append(chunk)
                length = sum(len(item) for item in review_chunks)
                if length % 100 < 20 or len(chunk) > 50:
                    await persist_progress(review_feedback=''.join(review_chunks))

            async def reset_review():
                review_chunks.clear()
                await persist_async(review_feedback='')

            current_stage = 'reviewer'
            try:
                if task.output_mode == 'stream':
                    review_result = loop.run_until_complete(_run_provider_stage(
                        task_pk,
                        claim_id,
                        worker_claim_id,
                        current_stage,
                        review_timeout,
                        lambda: AIModelService.review_test_cases_stream(
                            task, generated_cases, callback=review_callback
                        ),
                        reset_review,
                    ))
                    review_feedback = ''.join(review_chunks) or review_result
                else:
                    review_feedback = loop.run_until_complete(_run_provider_stage(
                        task_pk,
                        claim_id,
                        worker_claim_id,
                        current_stage,
                        review_timeout,
                        lambda: AIModelService.review_test_cases(task, generated_cases),
                        reset_review,
                    ))
            except GenerationClaimLost:
                raise
            except Exception as exc:
                review_feedback = f'评审过程出现异常: {exc}\n\n自动评审未完成，请人工检查原始用例。'
                logger.warning('Testcase review failed; keeping generated cases: task_id=%s', task_pk, exc_info=True)
                final_cases = _finalize_cases(generated_cases)
                persist(review_feedback=review_feedback, final_test_cases=final_cases)
            else:
                persist(
                    review_feedback=review_feedback,
                    status='revising',
                    progress=85,
                    final_test_cases='',
                )
                final_buffer = ''

                async def final_callback(chunk):
                    nonlocal final_buffer
                    final_buffer += chunk
                    if len(final_buffer) % 100 < 20 or len(chunk) > 50:
                        await persist_progress(final_test_cases=final_buffer)

                async def reset_revision():
                    nonlocal final_buffer
                    final_buffer = ''
                    await persist_async(final_test_cases='')

                current_stage = 'revision'
                try:
                    revised_cases = loop.run_until_complete(_run_provider_stage(
                        task_pk,
                        claim_id,
                        worker_claim_id,
                        current_stage,
                        review_timeout,
                        lambda: AIModelService.revise_test_cases_based_on_review(
                            task, generated_cases, review_feedback, callback=final_callback
                        ),
                        reset_revision,
                    ))
                    if revised_cases:
                        final_raw_output = getattr(revised_cases, 'raw_output', None) or str(revised_cases)
                        final_cases = _finalize_cases(revised_cases, fix_incomplete=True)
                    else:
                        final_raw_output = final_buffer
                        final_cases = final_buffer
                except GenerationClaimLost:
                    raise
                except Exception as exc:
                    logger.warning('Testcase revision failed; keeping generated cases: task_id=%s', task_pk, exc_info=True)
                    final_cases = _finalize_cases(generated_cases)
                    persist(review_feedback=f'{review_feedback}\n\n改进失败: {exc}，已保留原始用例。')
        else:
            final_cases = _finalize_cases(generated_cases)

        if final_cases is None:
            final_cases = _finalize_cases(generated_cases)
        canonical_cases = parse_generated_cases(final_cases)
        if not canonical_cases:
            persist(final_test_cases=final_raw_output)
            raise ValueError('最终生成内容无法解析为测试用例')
        try:
            validate_executable_cases(canonical_cases)
        except AIContractError as exc:
            persist(final_test_cases=final_raw_output)
            raise ValueError(f'最终生成用例字段不完整: {exc}') from exc
        persist(
            worker_claim_id='',
            status='completed',
            progress=100,
            completed_at=timezone.now(),
            final_test_cases=final_cases,
            error_message='',
        )
        return {'task_id': task_pk, 'status': 'completed'}
    except GenerationClaimLost:
        logger.info('Generation worker stopped after claim loss: task_id=%s stage=%s', task_pk, current_stage)
        return {'task_id': task_pk, 'status': 'superseded'}
    except Exception as exc:
        logger.exception('Testcase generation failed: task_id=%s stage=%s', task_pk, current_stage)
        try:
            _update_claimed(
                task_pk,
                claim_id,
                worker_claim_id,
                status='failed',
                worker_claim_id='',
                error_message=f'{current_stage}: {exc}',
            )
        except GenerationClaimLost:
            logger.info('Failure result ignored after cancellation/supersession: task_id=%s', task_pk)
        return {'task_id': task_pk, 'status': 'failed', 'stage': current_stage}
    finally:
        if loop is not None:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                logger.warning('Unable to shut down generation async generators: task_id=%s', task_pk, exc_info=True)
            loop.close()
            asyncio.set_event_loop(None)
        close_old_connections()


async def _reset_noop():
    return None
