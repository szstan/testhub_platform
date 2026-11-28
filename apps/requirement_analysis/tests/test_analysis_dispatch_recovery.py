from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, close_old_connections
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.requirement_analysis.models import AnalysisTask, BusinessRequirement, RequirementAnalysis, RequirementDocument
from apps.requirement_analysis.services import RequirementAnalysisService
from apps.requirement_analysis.tasks import process_requirement_document_task
from apps.users.models import User


pytestmark = pytest.mark.django_db
ANALYSIS_RESULT = {'analysis_report': 'Completed report', 'requirements_count': 0, 'requirements': []}


@pytest.fixture
def analysis_document():
    owner = User.objects.create_user(username='retryable-analysis-owner')
    project = Project.objects.create(name='Retryable analysis project', owner=owner)
    document = RequirementDocument.objects.create(
        title='Retryable requirements', document_type='txt', project=project,
        uploaded_by=owner, file='', extracted_text='Requirements text',
    )
    client = APIClient()
    client.force_authenticate(owner)
    return document, client


def test_failed_publish_reuses_persisted_pending_claim_when_retried(analysis_document):
    document, client = analysis_document
    url = f'/api/requirement-analysis/documents/{document.pk}/analyze/'
    with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay', side_effect=RuntimeError('broker unavailable')):
        response = client.post(url, {}, format='multipart')
    assert response.status_code == 500
    document.refresh_from_db()
    assert document.status == 'analyzing'
    claim = AnalysisTask.objects.get(document=document)
    assert claim.status == 'pending'
    assert claim.dispatch_state == 'publishing'
    with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay', return_value=SimpleNamespace(id='retried-task')) as publish:
        retried = client.post(url, {}, format='multipart')
    assert retried.status_code == 202, retried.data
    publish.assert_called_once_with(document.pk, claim.task_id, 0)
    assert AnalysisTask.objects.filter(document=document).count() == 1


def test_late_publish_error_does_not_overwrite_finished_analysis(analysis_document):
    document, client = analysis_document
    url = f'/api/requirement-analysis/documents/{document.pk}/analyze/'

    def already_completed(document_id, claim_id, _base_attempt):
        RequirementAnalysis.objects.create(document_id=document_id, analysis_report='Completed report')
        AnalysisTask.objects.filter(task_id=claim_id).update(status='completed')
        RequirementDocument.objects.filter(pk=document_id).update(status='analyzed')
        raise RuntimeError('publisher response was lost')

    with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay', side_effect=already_completed) as publish:
        response = client.post(url, {}, format='multipart')
        assert response.status_code == 500
        existing = client.post(url, {}, format='multipart')
    document.refresh_from_db()
    assert document.status == 'analyzed'
    assert existing.status_code == 200
    assert existing.data['analysis_id'] == document.analysis.pk
    assert publish.call_count == 1


def test_late_publish_error_does_not_reset_an_active_analysis_worker(analysis_document):
    document, client = analysis_document

    def already_started(_document_id, claim_id, _base_attempt):
        AnalysisTask.objects.filter(task_id=claim_id).update(status='running')
        raise RuntimeError('publisher response was lost')

    with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay', side_effect=already_started):
        response = client.post(f'/api/requirement-analysis/documents/{document.pk}/analyze/', {}, format='multipart')
    assert response.status_code == 500
    document.refresh_from_db()
    assert document.status == 'analyzing'
    assert AnalysisTask.objects.get(document=document).status == 'running'


@pytest.mark.parametrize('retry_first', [False, True])
def test_ambiguous_publish_and_explicit_retry_share_one_analysis_and_one_ai_call(analysis_document, retry_first):
    document, client = analysis_document
    url = f'/api/requirement-analysis/documents/{document.pk}/analyze/'
    deliveries = []

    def ambiguous_publish(*args):
        deliveries.append(args)
        raise RuntimeError('broker accepted but publisher response was lost')

    def retry_publish(*args):
        deliveries.append(args)
        return SimpleNamespace(id='retry-delivery')

    with patch.object(process_requirement_document_task, 'delay', side_effect=ambiguous_publish):
        assert client.post(url, {}, format='multipart').status_code == 500
    with patch.object(process_requirement_document_task, 'delay', side_effect=retry_publish):
        assert client.post(url, {}, format='multipart').status_code == 202
    assert deliveries[0] == deliveries[1]
    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock, return_value=ANALYSIS_RESULT) as analyze:
        first = process_requirement_document_task.run(*deliveries[int(retry_first)])
        duplicate = process_requirement_document_task.run(*deliveries[int(not retry_first)])
    analyze.assert_awaited_once_with('Requirements text', 'Retryable requirements')
    assert first['analysis_id'] == duplicate['analysis_id']
    assert AnalysisTask.objects.filter(document=document).count() == 1
    assert AnalysisTask.objects.get(document=document).status == 'completed'
    assert RequirementAnalysis.objects.filter(document=document).count() == 1
    document.refresh_from_db()
    assert document.status == 'analyzed'


@pytest.mark.django_db(transaction=True)
def test_same_claim_duplicate_delivery_cannot_analyze_while_first_worker_is_inflight(analysis_document):
    document, _client = analysis_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    started, release = Event(), Event()

    async def blocked_analysis(_text, _title):
        started.set()
        if not release.wait(timeout=10):
            raise TimeoutError('test did not release analysis')
        return ANALYSIS_RESULT

    def run_worker():
        close_old_connections()
        try:
            return process_requirement_document_task.run(document.pk, claim.task_id)
        finally:
            close_old_connections()

    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock, side_effect=blocked_analysis) as analyze:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(run_worker)
            try:
                assert started.wait(timeout=5)
                duplicate = process_requirement_document_task.run(document.pk, claim.task_id)
                assert duplicate['status'] == 'not_claimed'
                release.set()
                assert first.result(timeout=10)['status'] == 'completed'
            finally:
                release.set()
    assert analyze.await_count == 1
    assert AnalysisTask.objects.filter(document=document).count() == 1
    assert RequirementAnalysis.objects.filter(document=document).count() == 1


def test_pending_claim_blocks_source_replacement_after_an_ambiguous_publish(analysis_document):
    document, client = analysis_document
    with patch.object(process_requirement_document_task, 'delay', side_effect=RuntimeError('lost publisher reply')):
        assert client.post(f'/api/requirement-analysis/documents/{document.pk}/analyze/', {}, format='multipart').status_code == 500
    response = client.patch(f'/api/requirement-analysis/documents/{document.pk}/', {
        'file': SimpleUploadedFile('replacement.txt', b'Changed requirements'),
    }, format='multipart')
    assert response.status_code == 400, response.data
    document.refresh_from_db()
    assert document.extracted_text == 'Requirements text'
    assert document.file.name == ''


def test_legacy_delivery_also_reuses_completed_analysis_without_a_second_task(analysis_document):
    document, _client = analysis_document
    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock, return_value=ANALYSIS_RESULT) as analyze:
        first = process_requirement_document_task.run(document.pk)
        duplicate = process_requirement_document_task.run(document.pk)
    assert first['analysis_id'] == duplicate['analysis_id']
    analyze.assert_awaited_once()
    assert AnalysisTask.objects.filter(document=document).count() == 1


def test_finalization_failure_rolls_back_partial_analysis_and_old_claim_cannot_restart_it(analysis_document):
    document, _client = analysis_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    requirement = {'requirement_id': 'REQ-1', 'requirement_name': 'First', 'requirement_type': 'functional', 'module': 'Core', 'requirement_level': 'high', 'description': 'Description', 'acceptance_criteria': 'Works'}
    invalid = {**ANALYSIS_RESULT, 'requirements_count': 2, 'requirements': [requirement, requirement]}
    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock, return_value=invalid) as analyze:
        with pytest.raises(IntegrityError):
            RequirementAnalysisService._process_document_analysis(document.pk, claim.task_id)
        assert process_requirement_document_task.run(document.pk, claim.task_id)['status'] == 'not_claimed'
    analyze.assert_awaited_once()
    assert not RequirementAnalysis.objects.filter(document=document).exists()
    assert not BusinessRequirement.objects.exists()
    document.refresh_from_db()
    claim.refresh_from_db()
    assert document.status == 'analyzing'
    assert claim.status == 'pending'
    assert claim.retry_attempt == 1


def test_replaying_persisted_pending_submission_after_web_crash_republishes_same_claim(analysis_document):
    document, client = analysis_document
    claim = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    RequirementDocument.objects.filter(pk=document.pk).update(status='analyzing')
    with patch.object(process_requirement_document_task, 'delay', return_value=SimpleNamespace(id='replayed-after-web-crash')) as publish:
        response = client.post(f'/api/requirement-analysis/documents/{document.pk}/analyze/', {}, format='multipart')
    assert response.status_code == 202, response.data
    publish.assert_called_once_with(document.pk, claim.task_id, 0)
    assert AnalysisTask.objects.filter(document=document).count() == 1
    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock, return_value=ANALYSIS_RESULT) as analyze:
        first = process_requirement_document_task.run(document.pk, claim.task_id)
        second = process_requirement_document_task.run(document.pk, claim.task_id)
    assert first['analysis_id'] == second['analysis_id']
    analyze.assert_awaited_once()


def test_pending_delivery_cannot_run_alongside_a_legacy_active_claim(analysis_document):
    document, _client = analysis_document
    active = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    AnalysisTask.objects.filter(pk=active.pk).update(status='running')
    pending = RequirementAnalysisService.create_analysis_task(document, 'requirement_analysis')
    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock) as analyze:
        assert process_requirement_document_task.run(document.pk, pending.task_id)['status'] == 'not_claimed'
    analyze.assert_not_awaited()
    pending.refresh_from_db()
    assert pending.status == 'pending'
    assert not RequirementAnalysis.objects.filter(document=document).exists()


def test_analyze_returns_persisted_analysis_and_repairs_legacy_failed_status(analysis_document):
    document, client = analysis_document
    analysis = RequirementAnalysis.objects.create(document=document, analysis_report='Existing successful result')
    RequirementDocument.objects.filter(pk=document.pk).update(status='failed')
    with patch.object(process_requirement_document_task, 'delay') as publish:
        response = client.post(f'/api/requirement-analysis/documents/{document.pk}/analyze/', {}, format='multipart')
    assert response.status_code == 200, response.data
    assert response.data['analysis_id'] == analysis.pk
    publish.assert_not_called()
    document.refresh_from_db()
    assert document.status == 'analyzed'
