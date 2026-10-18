import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.requirement_analysis.models import RequirementDocument
from apps.users.models import User


pytestmark = pytest.mark.django_db


def test_requirement_public_ingest_requires_authentication():
    client = APIClient()

    text_response = client.post(
        '/api/requirement-analysis/analyze-text/',
        {'title': 'private', 'description': 'private requirement'},
        format='json',
    )
    upload_response = client.post(
        '/api/requirement-analysis/upload-and-analyze/',
        {
            'title': 'private',
            'file': SimpleUploadedFile('private.txt', b'private requirement', content_type='text/plain'),
        },
        format='multipart',
    )

    assert text_response.status_code == 401
    assert upload_response.status_code == 401
    assert RequirementDocument.objects.count() == 0


def test_requirement_documents_are_scoped_to_project_membership():
    owner = User.objects.create_user(username='requirement-owner', password='StrongPass123')
    outsider = User.objects.create_user(username='requirement-outsider', password='StrongPass123')
    project = Project.objects.create(name='private-requirement-project', owner=owner)
    RequirementDocument.objects.create(
        title='private requirement',
        file=SimpleUploadedFile('private.txt', b'private', content_type='text/plain'),
        document_type='txt',
        uploaded_by=owner,
        project=project,
        extracted_text='private',
    )

    client = APIClient()
    client.force_authenticate(user=outsider)
    response = client.get('/api/requirement-analysis/documents/')

    assert response.status_code == 200
    assert response.json()['count'] == 0
