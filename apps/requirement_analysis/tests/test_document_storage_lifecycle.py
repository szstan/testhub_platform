import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.requirement_analysis.models import AnalysisTask, RequirementDocument
from apps.requirement_analysis.serializers import RequirementDocumentSerializer
from apps.requirement_analysis.services import cleanup_document_file_if_unreferenced
from apps.users.models import User


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def stored_document(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    owner = User.objects.create_user(username='stored-document-owner')
    project = Project.objects.create(name='Stored documents', owner=owner)
    document = RequirementDocument.objects.create(
        title='Source requirements',
        document_type='txt',
        project=project,
        uploaded_by=owner,
        file=SimpleUploadedFile('source.txt', b'original content'),
    )
    client = APIClient()
    client.force_authenticate(owner)
    return document, client


def test_successful_replacement_deletes_old_storage_key_after_commit(stored_document):
    document, client = stored_document
    old_name = document.file.name
    storage = document.file.storage
    assert storage.exists(old_name)

    response = client.patch(
        f'/api/requirement-analysis/documents/{document.pk}/',
        {'file': SimpleUploadedFile('replacement.txt', b'replacement content')},
        format='multipart',
    )
    assert response.status_code == 200, response.data
    document.refresh_from_db()
    assert document.file.name != old_name
    assert not storage.exists(old_name)
    assert storage.exists(document.file.name)
    assert document.extracted_text == ''


def test_replacement_preserves_a_shared_key_until_its_last_reference_is_deleted(stored_document):
    document, client = stored_document
    old_name = document.file.name
    storage = document.file.storage
    other = RequirementDocument.objects.create(
        title='Second reference',
        document_type='txt',
        project=document.project,
        uploaded_by=document.uploaded_by,
        file=old_name,
    )

    response = client.patch(
        f'/api/requirement-analysis/documents/{document.pk}/',
        {'file': SimpleUploadedFile('replacement.txt', b'new source')},
        format='multipart',
    )
    assert response.status_code == 200, response.data
    assert storage.exists(old_name)
    assert RequirementDocument.objects.filter(file=old_name).count() == 1

    deleted = client.delete(f'/api/requirement-analysis/documents/{other.pk}/')
    assert deleted.status_code == 204
    assert not storage.exists(old_name)


def test_rollback_keeps_the_still_referenced_source_file(stored_document):
    document, _client = stored_document
    old_name = document.file.name
    storage = document.file.storage
    serializer = RequirementDocumentSerializer(
        document,
        data={'file': SimpleUploadedFile('rolled-back.txt', b'uncommitted replacement')},
        partial=True,
    )
    assert serializer.is_valid(), serializer.errors

    with pytest.raises(RuntimeError, match='rollback'):
        with transaction.atomic():
            serializer.save()
            raise RuntimeError('rollback replacement')

    document.refresh_from_db()
    assert document.file.name == old_name
    assert storage.exists(old_name)


def test_stale_cleanup_callback_rechecks_current_references(stored_document):
    document, _client = stored_document
    storage = document.file.storage
    shared_name = storage.save('reattached.txt', ContentFile(b'kept content'))

    with transaction.atomic():
        transaction.on_commit(
            lambda: cleanup_document_file_if_unreferenced(shared_name, storage=storage)
        )
        RequirementDocument.objects.filter(pk=document.pk).update(file=shared_name)

    assert storage.exists(shared_name)
    assert RequirementDocument.objects.filter(file=shared_name).exists()


def test_active_analysis_delete_is_rejected_then_terminal_delete_cleans_storage(stored_document):
    document, client = stored_document
    file_name = document.file.name
    storage = document.file.storage
    claim = AnalysisTask.objects.create(
        document=document,
        task_id='active-source-delete',
        task_type='requirement_analysis',
        status='pending',
    )
    RequirementDocument.objects.filter(pk=document.pk).update(status='analyzing')

    blocked = client.delete(f'/api/requirement-analysis/documents/{document.pk}/')
    assert blocked.status_code == 400
    assert RequirementDocument.objects.filter(pk=document.pk).exists()
    assert storage.exists(file_name)

    AnalysisTask.objects.filter(pk=claim.pk).update(status='failed')
    RequirementDocument.objects.filter(pk=document.pk).update(status='failed')
    deleted = client.delete(f'/api/requirement-analysis/documents/{document.pk}/')
    assert deleted.status_code == 204
    assert not RequirementDocument.objects.filter(pk=document.pk).exists()
    assert not storage.exists(file_name)
