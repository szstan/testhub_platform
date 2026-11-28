from io import BytesIO
from pathlib import Path
import subprocess
import sys
import zlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from reportlab.pdfgen.canvas import Canvas
from rest_framework.test import APIClient

from apps.requirement_analysis import pdf_worker
from apps.requirement_analysis.document_limits import (
    DocumentExtractionError, pdf_processing_limits,
)
from apps.requirement_analysis.models import RequirementDocument
from apps.requirement_analysis.pdf_worker import PdfWorkerFailure, execute_pdf_worker
from apps.requirement_analysis.services import DocumentProcessor
from apps.users.models import User


def make_pdf(text='', pages=1):
    buffer = BytesIO()
    canvas = Canvas(buffer)
    for _ in range(pages):
        if text:
            canvas.drawString(40, 740, text)
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def test_pdf_processing_limits_default_and_clamp(settings):
    limits = pdf_processing_limits()
    assert limits == {
        'max_pages': 500,
        'max_text_chars': 1_000_000,
        'max_file_bytes': 10 * 1024 * 1024,
        'timeout_seconds': 10,
        'memory_limit_bytes': 256 * 1024 * 1024,
    }

    settings.REQUIREMENT_PDF_MAX_PAGES = 10_000
    settings.REQUIREMENT_DOCUMENT_MAX_TEXT_CHARS = 12
    settings.REQUIREMENT_PDF_TIMEOUT_SECONDS = 90
    settings.REQUIREMENT_PDF_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
    limits = pdf_processing_limits()
    assert limits['max_pages'] == 500
    assert limits['max_text_chars'] == 12
    assert limits['timeout_seconds'] == 30
    assert limits['memory_limit_bytes'] == 256 * 1024 * 1024


def test_valid_pdf_is_extracted_in_child_process(tmp_path):
    path = tmp_path / 'valid.pdf'
    path.write_bytes(make_pdf('Login requires valid credentials.'))

    text = DocumentProcessor.extract_text_from_pdf(str(path))

    assert 'Login requires valid credentials.' in text


def test_pdf_page_and_text_caps_are_enforced(settings, tmp_path):
    path = tmp_path / 'many-pages.pdf'
    path.write_bytes(make_pdf('Requirement', pages=2))
    settings.REQUIREMENT_PDF_MAX_PAGES = 1
    with pytest.raises(DocumentExtractionError, match='页数超过限制'):
        DocumentProcessor.extract_text_from_pdf(str(path))

    path.write_bytes(make_pdf('A requirement longer than this cap.'))
    settings.REQUIREMENT_PDF_MAX_PAGES = 500
    settings.REQUIREMENT_DOCUMENT_MAX_TEXT_CHARS = 10
    with pytest.raises(DocumentExtractionError, match='文本长度超过限制'):
        DocumentProcessor.extract_text_from_pdf(str(path))


def test_valid_empty_pdf_is_rejected(tmp_path):
    path = tmp_path / 'scanned.pdf'
    path.write_bytes(make_pdf())

    with pytest.raises(DocumentExtractionError, match='没有可提取的文本'):
        DocumentProcessor.extract_text_from_pdf(str(path))


def test_pdf_worker_rejects_busy_work_without_queueing(tmp_path):
    assert pdf_worker._PDF_ADMISSION.acquire(blocking=False)
    try:
        with pytest.raises(PdfWorkerFailure, match='busy'):
            execute_pdf_worker(
                tmp_path / 'unused.pdf', max_pages=10, max_text_chars=100,
                max_file_bytes=1024, timeout_seconds=1,
                memory_limit_bytes=64 * 1024 * 1024,
            )
    finally:
        pdf_worker._PDF_ADMISSION.release()


def test_pdf_worker_timeout_kills_and_waits_for_child(monkeypatch, tmp_path):
    path = tmp_path / 'unused.pdf'
    path.write_bytes(b'%PDF-1.4')
    spawned = []
    real_popen = subprocess.Popen

    def track_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(pdf_worker.subprocess, 'Popen', track_popen)
    monkeypatch.setattr(
        pdf_worker, '_worker_command',
        lambda *_args: [sys.executable, '-I', '-c', 'import time; time.sleep(30)'],
    )

    with pytest.raises(PdfWorkerFailure, match='timeout'):
        execute_pdf_worker(
            path, max_pages=10, max_text_chars=100, max_file_bytes=1024,
            timeout_seconds=1, memory_limit_bytes=64 * 1024 * 1024,
        )

    assert len(spawned) == 1
    assert spawned[0].poll() is not None


def test_child_hard_memory_limit_blocks_safe_allocation():
    worker_dir = str(Path(pdf_worker.__file__).resolve().parent)
    code = (
        'import sys\n'
        f'sys.path.insert(0, {worker_dir!r})\n'
        'from pdf_worker import _install_resource_limits\n'
        '_install_resource_limits(64 * 1024 * 1024, 5)\n'
        'print("LIMIT_INSTALLED", flush=True)\n'
        'try:\n'
        '    data = bytearray(96 * 1024 * 1024)\n'
        '    for offset in range(0, len(data), 4096): data[offset] = 1\n'
        '    print("ALLOCATION_SUCCEEDED", flush=True)\n'
        'except MemoryError:\n'
        '    print("ALLOCATION_BLOCKED", flush=True)\n'
    )
    completed = subprocess.run(
        [sys.executable, '-I', '-c', code],
        capture_output=True, timeout=15,
        env=pdf_worker._worker_environment(),
    )
    output = completed.stdout.decode('utf-8', errors='replace')
    assert 'LIMIT_INSTALLED' in output, completed.stderr.decode('utf-8', errors='replace')
    assert 'ALLOCATION_SUCCEEDED' not in output
    if completed.returncode == 0:
        assert 'ALLOCATION_BLOCKED' in output


def test_small_compressed_pdf_expansion_is_bounded_by_worker_timeout(tmp_path, settings):
    # The decoded stream is deliberately only 2 MiB; the test never expands a huge payload in the host.
    path = tmp_path / 'small-expansion.pdf'
    settings.REQUIREMENT_DOCUMENT_MAX_TEXT_CHARS = 128
    settings.REQUIREMENT_PDF_MAX_TEXT_CHARS = 128
    settings.REQUIREMENT_PDF_TIMEOUT_SECONDS = 1

    from PyPDF2 import PdfWriter
    from PyPDF2.generic import DictionaryObject, EncodedStreamObject, NameObject

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    page = writer.pages[0]
    font = DictionaryObject()
    font[NameObject('/Type')] = NameObject('/Font')
    font[NameObject('/Subtype')] = NameObject('/Type1')
    font[NameObject('/BaseFont')] = NameObject('/Helvetica')
    font_reference = writer._add_object(font)
    fonts = DictionaryObject()
    fonts[NameObject('/F1')] = font_reference
    resources = DictionaryObject()
    resources[NameObject('/Font')] = fonts
    page[NameObject('/Resources')] = resources

    stream = EncodedStreamObject()
    stream._data = zlib.compress(
        b'BT /F1 12 Tf 0 0 Td (' + (b'A' * (2 * 1024 * 1024)) + b') Tj ET',
    )
    stream[NameObject('/Filter')] = NameObject('/FlateDecode')
    page[NameObject('/Contents')] = writer._add_object(stream)
    with path.open('wb') as output:
        writer.write(output)

    assert path.stat().st_size < 100_000
    with pytest.raises(DocumentExtractionError, match='超时|资源限制'):
        DocumentProcessor.extract_text_from_pdf(str(path))


@pytest.mark.django_db
def test_pdf_extraction_failure_does_not_cache_partial_text(settings):
    user = User.objects.create_user(username='pdf-no-partial-cache', password='StrongPass123')
    document = RequirementDocument.objects.create(
        title='Over budget PDF',
        file=SimpleUploadedFile(
            'over-budget.pdf', make_pdf('This text exceeds the test budget.'),
            content_type='application/pdf',
        ),
        document_type='pdf', uploaded_by=user,
    )
    settings.REQUIREMENT_DOCUMENT_MAX_TEXT_CHARS = 10
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get(f'/api/requirement-analysis/documents/{document.pk}/extract_text/')

    assert response.status_code == 400
    document.refresh_from_db()
    assert document.extracted_text == ''
