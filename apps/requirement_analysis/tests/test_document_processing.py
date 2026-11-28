from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.requirement_analysis import services
from apps.requirement_analysis.models import RequirementDocument
from apps.requirement_analysis.pdf_worker import PdfWorkerFailure, extract_pdf_text
from apps.requirement_analysis.services import DocumentExtractionError, DocumentProcessor
from apps.users.models import User


pytestmark = pytest.mark.django_db


def make_document(path, document_type):
    return SimpleNamespace(file=SimpleNamespace(path=str(path)), document_type=document_type)


def test_pdf_without_parser_dependency_fails_explicitly(monkeypatch, tmp_path):
    monkeypatch.setattr(services, 'PdfReader', None)

    with pytest.raises(DocumentExtractionError, match='依赖未安装'):
        DocumentProcessor.extract_text_from_pdf(str(tmp_path / 'unused.pdf'))


def test_empty_or_scanned_pdf_pages_fail_without_stringifying_none():
    class EmptyPage:
        def extract_text(self):
            return None

    class EmptyPdf:
        def __init__(self, _source):
            self.pages = [EmptyPage()]

    with pytest.raises(PdfWorkerFailure, match='empty'):
        extract_pdf_text(EmptyPdf, None, max_pages=10, max_text_chars=100)


def test_pdf_legacy_reader_extract_method_remains_supported():
    class LegacyPage:
        def extractText(self):
            return 'Legacy PDF requirement'

    class LegacyPdf:
        def __init__(self, _source):
            self.pages = [LegacyPage()]

    assert extract_pdf_text(
        LegacyPdf, None, max_pages=10, max_text_chars=100,
    ) == 'Legacy PDF requirement'


def test_corrupt_pdf_and_docx_fail_explicitly(monkeypatch, tmp_path):

    path = tmp_path / 'broken.pdf'
    path.write_bytes(b'not a pdf')

    with pytest.raises(DocumentExtractionError, match='PDF文件损坏'):
        DocumentProcessor.extract_text(make_document(path, 'pdf'))

    monkeypatch.setattr(services, 'docx', SimpleNamespace(Document=Mock(side_effect=ValueError('bad zip'))))
    with pytest.raises(DocumentExtractionError, match='DOCX文件损坏'):
        DocumentProcessor.extract_text(make_document(path, 'docx'))


def test_missing_docx_dependency_fails_explicitly(monkeypatch, tmp_path):
    monkeypatch.setattr(services, 'docx', None)

    with pytest.raises(DocumentExtractionError, match='依赖未安装'):
        DocumentProcessor.extract_text(make_document(tmp_path / 'unused.docx', 'docx'))


def test_docx_extracts_paragraph_and_table_content(monkeypatch, tmp_path):
    table = SimpleNamespace(rows=[SimpleNamespace(cells=[
        SimpleNamespace(text='Field'), SimpleNamespace(text='Required'),
    ])])
    document = SimpleNamespace(
        paragraphs=[SimpleNamespace(text='User login requirement')],
        tables=[table],
    )
    monkeypatch.setattr(services, 'docx', SimpleNamespace(Document=Mock(return_value=document)))
    archive_path = tmp_path / 'requirements.docx'
    with zipfile.ZipFile(archive_path, 'w') as archive:
        archive.writestr('word/document.xml', '<document/>')

    text = DocumentProcessor.extract_text_from_docx(str(archive_path))

    assert text == 'User login requirement\nField | Required'


def test_unrecognized_text_encoding_fails_but_gbk_text_remains_supported(tmp_path):
    invalid_path = tmp_path / 'invalid.txt'
    invalid_path.write_bytes(b'\x810')
    gbk_path = tmp_path / 'legacy.txt'
    gbk_path.write_bytes('登录需求'.encode('gbk'))

    with pytest.raises(DocumentExtractionError, match='编码无法识别'):
        DocumentProcessor.extract_text_from_txt(str(invalid_path))
    assert DocumentProcessor.extract_text_from_txt(str(gbk_path)) == '登录需求'


def test_extract_endpoint_returns_client_error_and_does_not_cache_failure():
    user = User.objects.create_user(username='document-extraction', password='StrongPass123')
    document = RequirementDocument.objects.create(
        title='Undecodable requirement',
        file=SimpleUploadedFile('broken.txt', b'\x810', content_type='text/plain'),
        document_type='txt',
        uploaded_by=user,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get(f'/api/requirement-analysis/documents/{document.pk}/extract_text/')

    assert response.status_code == 400
    assert 'error' in response.json()
    document.refresh_from_db()
    assert document.extracted_text == ''


def test_extract_endpoint_replaces_legacy_cached_error_with_valid_text():
    user = User.objects.create_user(username='document-legacy-cache', password='StrongPass123')
    document = RequirementDocument.objects.create(
        title='Legacy cache',
        file=SimpleUploadedFile('requirement.txt', b'Login must require a password.', content_type='text/plain'),
        document_type='txt',
        uploaded_by=user,
        extracted_text='文本文件读取失败: bad encoding',
    )
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get(f'/api/requirement-analysis/documents/{document.pk}/extract_text/')

    assert response.status_code == 200
    assert response.json()['extracted_text'] == 'Login must require a password.'
    document.refresh_from_db()
    assert document.extracted_text == 'Login must require a password.'
