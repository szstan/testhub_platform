"""Background tasks for requirement document analysis."""

from asgiref.sync import async_to_sync
from celery import shared_task

from .models import RequirementDocument
from .services import RequirementAnalysisService


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def process_requirement_document_task(self, document_id):
    document = RequirementDocument.objects.get(pk=document_id)
    return async_to_sync(RequirementAnalysisService.process_document_analysis)(document)
