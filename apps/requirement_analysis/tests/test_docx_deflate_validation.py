"""Tiny malformed-DEFLATE DOCX uploads fail as client validation errors."""
from io import BytesIO
import zipfile
import zlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.requirement_analysis.document_limits import DocumentExtractionError, validate_docx_archive
from apps.requirement_analysis.models import RequirementDocument
from apps.users.models import User


def malformed_deflate_docx():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', b'<document>Small fixture</document>')
    raw = bytearray(buffer.getvalue())
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        member = archive.infolist()[0]
        offset = member.header_offset + 30 + len(member.filename.encode('utf-8')) + len(member.extra)
    # BFINAL=1 and reserved BTYPE=3: invalid immediately, no expansion is possible.
    raw[offset] = 0x07
    return bytes(raw)


def test_malformed_deflate_is_wrapped_and_upload_stream_position_is_restored():
    raw = malformed_deflate_docx()
    with zipfile.ZipFile(BytesIO(raw)) as archive, pytest.raises(zlib.error):
        archive.read('word/document.xml')
    stream = BytesIO(raw)
    with pytest.raises(DocumentExtractionError, match='DOCX文件损坏') as error:
        validate_docx_archive(stream)
    assert isinstance(error.value.__cause__, zlib.error)
    assert stream.tell() == 0


@pytest.mark.django_db
def test_malformed_deflate_upload_returns400_without_persisting_document():
    owner = User.objects.create_user(username='malformed-docx-owner')
    project = Project.objects.create(name='Malformed DOCX project', owner=owner)
    client = APIClient()
    client.force_authenticate(owner)
    response = client.post('/api/requirement-analysis/documents/', {
        'title': 'Malformed archive', 'project': project.pk,
        'file': SimpleUploadedFile('malformed.docx', malformed_deflate_docx()),
    }, format='multipart')
    assert response.status_code == 400, response.data
    assert 'DOCX文件损坏' in str(response.data)
    assert not RequirementDocument.objects.exists()
