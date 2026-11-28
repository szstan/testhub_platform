from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from celery.exceptions import Retry
from django.db import OperationalError
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.requirement_analysis.models import AnalysisTask, RequirementAnalysis, RequirementDocument
from apps.requirement_analysis.tasks import process_requirement_document_task
from apps.users.models import User


pytestmark = pytest.mark.django_db
ANALYSIS_RESULT = {'analysis_report': 'Recovered report', 'requirements_count': 0, 'requirements': []}


@pytest.fixture
def retry_document():
    owner = User.objects.create_user(username='analysis-retry-owner')
    project = Project.objects.create(name='Analysis retry project', owner=owner)
    document = RequirementDocument.objects.create(
        title='Transient requirements', document_type='txt', project=project,
        uploaded_by=owner, file='', extracted_text='Requirements text',
    )
    client = APIClient()
    client.force_authenticate(owner)
    return document, client


def _run_with_retries(task, retries, *args, **kwargs):
    task.push_request(retries=retries)
    try:
        return task.run(*args, **kwargs)
    finally:
        task.pop_request()


def test_transient_failure_retries_same_legacy_claim_and_stale_delivery_is_fenced(retry_document):
    document, _client = retry_document
    with patch(
        'apps.requirement_analysis.services.AIService.analyze_requirements',
        new_callable=AsyncMock,
        side_effect=[RuntimeError('temporary AI outage'), ANALYSIS_RESULT],
    ) as analyze:
        scheduled = Retry('scheduled retry')
        with patch.object(process_requirement_document_task, 'retry', side_effect=scheduled) as retry:
            with pytest.raises(Retry):
                process_requirement_document_task.run(document.pk)

        claim = AnalysisTask.objects.get(document=document)
        assert claim.status == 'pending'
        assert claim.retry_attempt == 1
        document.refresh_from_db()
        assert document.status == 'analyzing'
        assert retry.call_args.kwargs['args'] == (document.pk, claim.task_id, 0)

        stale = process_requirement_document_task.run(document.pk)
        assert stale['status'] == 'not_claimed'
        assert analyze.await_count == 1

        result = _run_with_retries(
            process_requirement_document_task, 1, document.pk, claim.task_id, 0,
        )

    assert result['status'] == 'completed'
    assert analyze.await_count == 2
    assert RequirementAnalysis.objects.filter(document=document).count() == 1
    claim.refresh_from_db()
    assert claim.status == 'completed'
    assert claim.retry_attempt == 1
    assert AnalysisTask.objects.filter(document=document).count() == 1
    document.refresh_from_db()
    assert document.status == 'analyzed'


def test_three_attempt_budget_survives_resubmits_and_terminal_failure_allows_new_claim(retry_document):
    document, client = retry_document
    url = f'/api/requirement-analysis/documents/{document.pk}/analyze/'
    with patch.object(
        process_requirement_document_task, 'delay', return_value=SimpleNamespace(id='initial-delivery'),
    ) as publish:
        response = client.post(url, {}, format='multipart')
    assert response.status_code == 202, response.data
    claim_id = response.data['analysis_task_id']
    assert publish.call_args.args == (document.pk, claim_id, 0)

    ai_calls = 0
    for attempt in range(2):
        with patch(
            'apps.requirement_analysis.services.AIService.analyze_requirements',
            new_callable=AsyncMock,
            side_effect=RuntimeError(f'transient failure {attempt}'),
        ) as analyze:
            scheduled = Retry('scheduled retry')
            with patch.object(process_requirement_document_task, 'retry', side_effect=scheduled) as retry:
                with pytest.raises(Retry):
                    process_requirement_document_task.run(document.pk, claim_id, attempt)
            assert analyze.await_count == 1
            ai_calls += analyze.await_count
            assert retry.call_args.kwargs['args'] == (document.pk, claim_id, attempt)

        claim = AnalysisTask.objects.get(task_id=claim_id)
        assert claim.status == 'pending'
        assert claim.retry_attempt == attempt + 1
        document.refresh_from_db()
        assert document.status == 'analyzing'

        with patch.object(
            process_requirement_document_task, 'delay', return_value=SimpleNamespace(id=f'resubmit-{attempt}'),
        ) as publish:
            response = client.post(url, {}, format='multipart')
        assert response.status_code == 202, response.data
        assert response.data['analysis_task_id'] == claim_id
        assert publish.call_args.args == (document.pk, claim_id, attempt + 1)

    # The original attempt-zero delivery cannot consume the attempt-two claim.
    with patch(
        'apps.requirement_analysis.services.AIService.analyze_requirements',
        new_callable=AsyncMock,
        side_effect=RuntimeError('terminal failure'),
    ) as analyze:
        stale = process_requirement_document_task.run(document.pk, claim_id, 0)
        assert stale['status'] == 'not_claimed'
        analyze.assert_not_awaited()

        with patch.object(process_requirement_document_task, 'retry') as retry:
            with pytest.raises(RuntimeError, match='terminal failure'):
                process_requirement_document_task.run(document.pk, claim_id, 2)
        retry.assert_not_called()
        assert analyze.await_count == 1
        ai_calls += analyze.await_count

    claim = AnalysisTask.objects.get(task_id=claim_id)
    claim.refresh_from_db()
    document.refresh_from_db()
    assert ai_calls == 3
    assert claim.status == 'failed'
    assert claim.retry_attempt == 2
    assert document.status == 'failed'

    with patch.object(
        process_requirement_document_task, 'delay', return_value=SimpleNamespace(id='fresh-delivery'),
    ) as publish:
        response = client.post(url, {}, format='multipart')
    assert response.status_code == 202, response.data
    new_claim_id = response.data['analysis_task_id']
    assert new_claim_id != claim_id
    assert publish.call_args.args == (document.pk, new_claim_id, 0)
    assert AnalysisTask.objects.filter(document=document).count() == 2
    assert AnalysisTask.objects.get(task_id=new_claim_id).retry_attempt == 0


@pytest.mark.parametrize('fault_site', ['document_lookup', 'claim_transaction'])
def test_transient_preclaim_failure_does_not_discard_the_next_celery_attempt(retry_document, fault_site):
    document, _client = retry_document
    claim = AnalysisTask.objects.create(
        document=document, task_type='requirement_analysis', task_id=f'preclaim-{fault_site}',
    )
    target = ('apps.requirement_analysis.tasks.RequirementDocument.objects.filter'
              if fault_site == 'document_lookup'
              else 'apps.requirement_analysis.services.RequirementAnalysisService._process_document_analysis')
    with patch(target, side_effect=OperationalError('temporary database failure')):
        with patch.object(process_requirement_document_task, 'retry', side_effect=Retry('scheduled retry')) as retry:
            with pytest.raises(Retry):
                process_requirement_document_task.run(document.pk, claim.task_id, 0)
    claim.refresh_from_db()
    assert claim.status == 'pending'
    assert claim.retry_attempt == 0
    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock, return_value=ANALYSIS_RESULT) as analyze:
        retry_options = retry.call_args.kwargs
        result = _run_with_retries(
            process_requirement_document_task,
            1,
            *retry_options['args'],
            **retry_options['kwargs'],
        )
    analyze.assert_awaited_once()
    assert result['status'] == 'completed'
    claim.refresh_from_db()
    assert claim.retry_attempt == 0
    assert claim.status == 'completed'


@pytest.mark.parametrize('attempt', [-1, 3])
def test_attempt_outside_global_budget_cannot_start_pending_analysis(retry_document, attempt):
    document, _client = retry_document
    claim = AnalysisTask.objects.create(
        document=document, task_type='requirement_analysis', task_id=f'out-of-budget-{attempt}',
    )
    with patch('apps.requirement_analysis.services.AIService.analyze_requirements', new_callable=AsyncMock) as analyze:
        result = process_requirement_document_task.run(document.pk, claim.task_id, attempt)
    analyze.assert_not_awaited()
    assert result['status'] == 'not_claimed'
    claim.refresh_from_db()
    assert claim.status == 'pending'
    assert claim.retry_attempt == 0
