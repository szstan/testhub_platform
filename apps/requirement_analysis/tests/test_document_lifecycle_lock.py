from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.requirement_analysis.models import RequirementDocument
from apps.requirement_analysis.views import RequirementDocumentViewSet
from apps.users.models import User


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def lifecycle_document(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    owner = User.objects.create_user(username='document-lifecycle-owner')
    project = Project.objects.create(name='Document lifecycle', owner=owner)
    document = RequirementDocument.objects.create(
        title='Current requirements', document_type='txt', project=project,
        uploaded_by=owner, file=SimpleUploadedFile('original.txt', b'old requirements'),
    )
    client = APIClient()
    client.force_authenticate(owner)
    return document, client


def test_replacement_rechecks_analysis_state_after_locking_current_document(lifecycle_document):
    document, client = lifecycle_document
    old_name = document.file.name
    stale = RequirementDocument.objects.get(pk=document.pk)
    # Model the state committed by the competing request after the scoped read.
    RequirementDocument.objects.filter(pk=document.pk).update(status='analyzing')

    with patch.object(RequirementDocumentViewSet, 'get_object', return_value=stale):
        response = client.patch(f'/api/requirement-analysis/documents/{document.pk}/', {
            'file': SimpleUploadedFile('replacement.txt', b'new requirements'),
        }, format='multipart')
    assert response.status_code == 400, response.data
    document.refresh_from_db()
    assert document.status == 'analyzing'
    assert document.file.name == old_name


def test_analysis_claim_does_not_restore_file_from_stale_scoped_read(lifecycle_document):
    document, client = lifecycle_document
    replacement = document.file.storage.save('replacement.txt', ContentFile(b'new requirements'))
    scoped_get = RequirementDocumentViewSet.get_object

    def replacement_wins(view):
        stale = scoped_get(view)
        RequirementDocument.objects.filter(pk=stale.pk).update(file=replacement, extracted_text='new cached requirements')
        return stale

    def dispatch(document_id, analysis_task_id, base_attempt):
        assert analysis_task_id
        assert base_attempt == 0
        current = RequirementDocument.objects.get(pk=document_id)
        assert current.file.name == replacement
        assert current.extracted_text == 'new cached requirements'
        return SimpleNamespace(id='lifecycle-analysis-task')

    with (
        patch.object(RequirementDocumentViewSet, 'get_object', replacement_wins),
        patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay', side_effect=dispatch),
    ):
        response = client.post(f'/api/requirement-analysis/documents/{document.pk}/analyze/', {}, format='multipart')
    assert response.status_code == 202, response.data
    document.refresh_from_db()
    assert document.file.name == replacement
    assert document.extracted_text == 'new cached requirements'
    assert document.status == 'analyzing'


def test_extraction_caches_only_the_locked_current_file(lifecycle_document):
    document, client = lifecycle_document
    replacement = document.file.storage.save('replacement.txt', ContentFile(b'new requirements'))
    scoped_get = RequirementDocumentViewSet.get_object

    def replacement_wins(view):
        stale = scoped_get(view)
        RequirementDocument.objects.filter(pk=stale.pk).update(file=replacement, extracted_text='')
        return stale

    with patch.object(RequirementDocumentViewSet, 'get_object', replacement_wins):
        response = client.get(f'/api/requirement-analysis/documents/{document.pk}/extract_text/')
    assert response.status_code == 200, response.data
    assert response.data['extracted_text'] == 'new requirements'
    document.refresh_from_db()
    assert document.file.name == replacement
    assert document.extracted_text == 'new requirements'


def test_deleted_document_in_lock_lookup_gap_returns_not_found(lifecycle_document):
    document, client = lifecycle_document
    stale = RequirementDocument.objects.get(pk=document.pk)
    RequirementDocument.objects.filter(pk=document.pk).delete()

    with patch.object(RequirementDocumentViewSet, 'get_object', return_value=stale):
        response = client.get(f'/api/requirement-analysis/documents/{document.pk}/extract_text/')
    assert response.status_code == 404
    assert not RequirementDocument.objects.filter(pk=document.pk).exists()
