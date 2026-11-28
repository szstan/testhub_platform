"""Role checks and expansion budgets on all document mutation paths."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.projects.models import Project, ProjectMember
from apps.requirement_analysis import services
from apps.requirement_analysis.document_limits import (
    DocumentExtractionError, validate_docx_archive,
)
from apps.requirement_analysis.pdf_worker import PdfWorkerFailure, extract_pdf_text
from apps.requirement_analysis.models import (
    BusinessRequirement, GeneratedTestCase, RequirementAnalysis, RequirementDocument,
)
from apps.requirement_analysis.services import DocumentProcessor
from apps.users.models import User


@pytest.fixture
def document_roles(db):
    owner = User.objects.create_user(username='document-budget-owner')
    viewer = User.objects.create_user(username='document-budget-viewer')
    tester = User.objects.create_user(username='document-budget-tester')
    project = Project.objects.create(name='Document budgets project', owner=owner)
    ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    ProjectMember.objects.create(project=project, user=tester, role='tester')
    document = RequirementDocument.objects.create(
        title='Protected document',
        file=SimpleUploadedFile('requirements.txt', b'Login must validate credentials.'),
        document_type='txt', project=project, uploaded_by=viewer,
    )
    client = APIClient()
    client.force_authenticate(viewer)
    return project, document, tester, client


def test_viewer_can_read_but_cannot_mutate_upload_or_analyze(document_roles):
    project, document, _tester, client = document_roles
    detail = f'/api/requirement-analysis/documents/{document.pk}/'
    assert client.get(detail).status_code == 200
    with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay') as dispatch:
        responses = [
            client.patch(detail, {'title': 'Forbidden edit'}, format='multipart'),
            client.delete(detail),
            client.post(detail + 'analyze/', {}, format='multipart'),
            client.post('/api/requirement-analysis/documents/', {
                'title': 'Forbidden upload', 'project': project.pk,
                'file': SimpleUploadedFile('new.txt', b'new document'),
            }, format='multipart'),
            client.post('/api/requirement-analysis/upload-and-analyze/', {
                'title': 'Forbidden immediate analysis', 'project': project.pk,
                'file': SimpleUploadedFile('new.txt', b'new document'),
            }, format='multipart'),
            client.post('/api/requirement-analysis/analyze-text/', {
                'title': 'Forbidden text analysis', 'description': 'private text',
                'project': project.pk,
            }, format='json'),
            client.get(detail + 'extract_text/'),
        ]
    assert [response.status_code for response in responses] == [403] * len(responses)
    dispatch.assert_not_called()
    document.refresh_from_db()
    assert document.title == 'Protected document'
    assert document.status == 'uploaded'
    assert document.extracted_text == ''
    assert RequirementDocument.objects.count() == 1


def test_viewer_can_read_cached_text_without_writing(document_roles):
    _project, document, _tester, client = document_roles
    document.extracted_text = 'Already extracted content'
    document.save(update_fields=['extracted_text'])
    with patch.object(DocumentProcessor, 'extract_text') as extract:
        response = client.get(f'/api/requirement-analysis/documents/{document.pk}/extract_text/')
    assert response.status_code == 200
    assert response.data['extracted_text'] == 'Already extracted content'
    extract.assert_not_called()


def test_tester_can_edit_upload_and_queue_analysis(document_roles):
    project, document, tester, client = document_roles
    client.force_authenticate(tester)
    detail = f'/api/requirement-analysis/documents/{document.pk}/'
    assert client.patch(detail, {'title': 'Tester edit'}, format='multipart').status_code == 200
    uploaded = client.post('/api/requirement-analysis/documents/', {
        'title': 'Tester upload', 'project': project.pk,
        'file': SimpleUploadedFile('new.txt', b'New document'),
    }, format='multipart')
    assert uploaded.status_code == 201, uploaded.data
    assert RequirementDocument.objects.get(pk=uploaded.data['id']).uploaded_by_id == tester.pk
    with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay', return_value=SimpleNamespace(id='test-task')) as dispatch:
        response = client.post(detail + 'analyze/', {}, format='multipart')
    assert response.status_code == 202, response.data
    dispatch.assert_called_once_with(
        document.pk, document.tasks.get(task_type='requirement_analysis').task_id, 0,
    )


@pytest.mark.parametrize(('document_status', 'has_analysis'), [
    ('analyzed', True),
    ('analyzing', False),
])
def test_file_replacement_is_rejected_after_analysis_starts(document_roles, document_status, has_analysis):
    _project, document, tester, client = document_roles
    document.status = document_status
    document.extracted_text = 'Text from the original document.'
    document.save(update_fields=['status', 'extracted_text'])
    analysis = None
    if has_analysis:
        analysis = RequirementAnalysis.objects.create(
            document=document, analysis_report='Analysis from the original document.')

    original_file = document.file.name
    client.force_authenticate(tester)
    response = client.patch(
        f'/api/requirement-analysis/documents/{document.pk}/',
        {'file': SimpleUploadedFile('replacement.txt', b'New source text.')},
        format='multipart',
    )

    assert response.status_code == 400, response.data
    document.refresh_from_db()
    assert document.file.name == original_file
    assert document.status == document_status
    assert document.extracted_text == 'Text from the original document.'
    if analysis:
        analysis.refresh_from_db()
        assert analysis.analysis_report == 'Analysis from the original document.'


def test_unanalyzed_document_file_can_be_replaced(document_roles):
    _project, document, tester, client = document_roles
    original_file = document.file.name
    new_content = b'Updated login requirements.'
    client.force_authenticate(tester)

    response = client.patch(
        f'/api/requirement-analysis/documents/{document.pk}/',
        {'file': SimpleUploadedFile('replacement.txt', new_content)},
        format='multipart',
    )

    assert response.status_code == 200, response.data
    document.refresh_from_db()
    assert document.file.name != original_file
    assert document.file_size == len(new_content)
    assert document.status == 'uploaded'
    assert document.extracted_text == ''
    with document.file.open('rb') as stream:
        assert stream.read() == new_content


def test_viewer_cannot_generate_legacy_cases_or_edit_generated_case(document_roles):
    _project, document, _tester, client = document_roles
    analysis = RequirementAnalysis.objects.create(document=document)
    requirement = BusinessRequirement.objects.create(
        analysis=analysis, requirement_id='REQ-1', requirement_name='Login',
    )
    case = GeneratedTestCase.objects.create(requirement=requirement, case_id='CASE-1', title='Existing case')
    generated = client.post('/api/requirement-analysis/requirements/generate_test_cases/', {
        'requirement_ids': [requirement.pk], 'test_case_count': 1,
    }, format='json')
    edited = client.patch(f'/api/requirement-analysis/test-cases/{case.pk}/', {
        'title': 'Forbidden case edit',
    }, format='json')
    assert generated.status_code == 403, generated.data
    assert edited.status_code == 403, edited.data
    assert GeneratedTestCase.objects.count() == 1
    case.refresh_from_db()
    assert case.title == 'Existing case'


def archive_bytes(entries):
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return stream.getvalue()


@pytest.mark.parametrize(('setting_name', 'limit', 'entries'), [
    ('REQUIREMENT_DOCX_MAX_MEMBERS', 2, [(f'{i}.xml', b'x') for i in range(3)]),
    ('REQUIREMENT_DOCX_MAX_MEMBER_BYTES', 128, [('word/document.xml', b'x' * 129)]),
    ('REQUIREMENT_DOCX_MAX_EXPANDED_BYTES', 200, [('first.xml', b'x' * 101), ('second.xml', b'x' * 100)]),
])
def test_docx_expansion_is_rejected_before_parser(settings, tmp_path, monkeypatch, setting_name, limit, entries):
    setattr(settings, setting_name, limit)
    path = tmp_path / 'oversized.docx'
    path.write_bytes(archive_bytes(entries))
    parser = Mock()
    monkeypatch.setattr(services, 'docx', SimpleNamespace(Document=parser))
    with pytest.raises(DocumentExtractionError, match='超过限制'):
        DocumentProcessor.extract_text_from_docx(str(path))
    parser.assert_not_called()


def test_docx_archive_validation_restores_uploaded_stream_position():
    stream = BytesIO(archive_bytes([('word/document.xml', b'<document/>')]))
    stream.seek(5)
    validate_docx_archive(stream)
    assert stream.tell() == 5


def test_docx_expansion_error_is_client_error_without_stored_document(document_roles, settings):
    project, _document, tester, client = document_roles
    settings.REQUIREMENT_DOCX_MAX_EXPANDED_BYTES = 100
    client.force_authenticate(tester)
    content = archive_bytes([('word/document.xml', b'x' * 101)])
    before = RequirementDocument.objects.count()
    with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay') as dispatch:
        response = client.post('/api/requirement-analysis/upload-and-analyze/', {
            'title': 'Expansion bomb', 'project': project.pk,
            'file': SimpleUploadedFile('oversized.docx', content),
        }, format='multipart')
    assert response.status_code == 400, response.data
    assert RequirementDocument.objects.count() == before
    dispatch.assert_not_called()


def test_extracted_text_budget_returns_client_error_without_cached_partial(document_roles, settings):
    _project, document, tester, client = document_roles
    client.force_authenticate(tester)
    settings.REQUIREMENT_DOCUMENT_MAX_TEXT_CHARS = 10
    response = client.get(f'/api/requirement-analysis/documents/{document.pk}/extract_text/')
    assert response.status_code == 400, response.data
    document.refresh_from_db()
    assert document.extracted_text == ''


def test_docx_table_text_and_pdf_text_share_incremental_limit(settings, tmp_path, monkeypatch):
    settings.REQUIREMENT_DOCUMENT_MAX_TEXT_CHARS = 10
    path = tmp_path / 'bounded.docx'
    path.write_bytes(archive_bytes([('word/document.xml', b'<document/>')]))
    fake_docx = SimpleNamespace(
        paragraphs=[SimpleNamespace(text='12345')],
        tables=[SimpleNamespace(rows=[SimpleNamespace(cells=[SimpleNamespace(text='67890')])])],
    )
    monkeypatch.setattr(services, 'docx', SimpleNamespace(Document=lambda _path: fake_docx))
    with pytest.raises(DocumentExtractionError, match='文本长度超过限制'):
        DocumentProcessor.extract_text_from_docx(str(path))
    fake_pdf = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: 'x' * 11)])
    with pytest.raises(PdfWorkerFailure, match='text_limit'):
        extract_pdf_text(lambda _source: fake_pdf, None, max_pages=10, max_text_chars=10)


def test_text_analysis_over_budget_never_writes_or_dispatches(document_roles, settings):
    project, _document, tester, client = document_roles
    client.force_authenticate(tester)
    settings.REQUIREMENT_DOCUMENT_MAX_TEXT_CHARS = 10
    before = RequirementDocument.objects.count()
    with patch('apps.requirement_analysis.tasks.process_requirement_document_task.delay') as dispatch:
        response = client.post('/api/requirement-analysis/analyze-text/', {
            'title': 'Text input', 'description': 'x' * 11, 'project': project.pk,
        }, format='json')
    assert response.status_code == 400
    assert RequirementDocument.objects.count() == before
    dispatch.assert_not_called()
