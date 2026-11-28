"""Bound document inputs before parsers allocate expanded archives or text."""

import os
import zipfile
import zlib

from django.conf import settings


class DocumentExtractionError(ValueError):
    """A document is invalid or cannot yield usable requirement text."""


_DEFAULTS = {
    'REQUIREMENT_DOCUMENT_MAX_FILE_BYTES': 10 * 1024 * 1024,
    'REQUIREMENT_DOCX_MAX_MEMBERS': 1024,
    'REQUIREMENT_DOCX_MAX_MEMBER_BYTES': 8 * 1024 * 1024,
    'REQUIREMENT_DOCX_MAX_EXPANDED_BYTES': 32 * 1024 * 1024,
    'REQUIREMENT_DOCUMENT_MAX_TEXT_CHARS': 1_000_000,
    'REQUIREMENT_PDF_MAX_PAGES': 500,
    'REQUIREMENT_PDF_MAX_TEXT_CHARS': 1_000_000,
    'REQUIREMENT_PDF_TIMEOUT_SECONDS': 10,
    'REQUIREMENT_PDF_MEMORY_LIMIT_BYTES': 256 * 1024 * 1024,
}


def document_limit(name):
    value = getattr(settings, name, _DEFAULTS[name])
    return value if type(value) is int and value > 0 else _DEFAULTS[name]


def pdf_processing_limits():
    """Return PDF limits, allowing settings to tighten but not disable hard caps."""
    return {
        'max_pages': min(
            document_limit('REQUIREMENT_PDF_MAX_PAGES'),
            _DEFAULTS['REQUIREMENT_PDF_MAX_PAGES'],
        ),
        'max_text_chars': min(
            document_limit('REQUIREMENT_PDF_MAX_TEXT_CHARS'),
            document_limit('REQUIREMENT_DOCUMENT_MAX_TEXT_CHARS'),
            _DEFAULTS['REQUIREMENT_PDF_MAX_TEXT_CHARS'],
        ),
        'max_file_bytes': min(
            document_limit('REQUIREMENT_DOCUMENT_MAX_FILE_BYTES'),
            _DEFAULTS['REQUIREMENT_DOCUMENT_MAX_FILE_BYTES'],
        ),
        'timeout_seconds': min(
            document_limit('REQUIREMENT_PDF_TIMEOUT_SECONDS'), 30,
        ),
        'memory_limit_bytes': min(
            document_limit('REQUIREMENT_PDF_MEMORY_LIMIT_BYTES'),
            _DEFAULTS['REQUIREMENT_PDF_MEMORY_LIMIT_BYTES'],
        ),
    }


def validate_docx_archive(source):
    """Check all advertised sizes and stream expanded members before DOCX parsing."""
    position = source.tell() if hasattr(source, 'tell') else None
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > document_limit('REQUIREMENT_DOCX_MAX_MEMBERS'):
                raise DocumentExtractionError('DOCX压缩包成员数量超过限制')
            total = 0
            expanded_limit = document_limit('REQUIREMENT_DOCX_MAX_EXPANDED_BYTES')
            member_limit = document_limit('REQUIREMENT_DOCX_MAX_MEMBER_BYTES')
            for entry in entries:
                if entry.file_size > member_limit:
                    raise DocumentExtractionError('DOCX单个成员展开大小超过限制')
                total += entry.file_size
                if total > expanded_limit:
                    raise DocumentExtractionError('DOCX展开总大小超过限制')
                if entry.flag_bits & 1 or entry.compress_type not in (
                    zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                ):
                    raise DocumentExtractionError('DOCX压缩格式不支持')
            actual_total = 0
            for entry in entries:
                actual_member = 0
                with archive.open(entry) as member:
                    while True:
                        chunk = member.read(64 * 1024)
                        if not chunk:
                            break
                        actual_member += len(chunk)
                        actual_total += len(chunk)
                        if actual_member > member_limit or actual_total > expanded_limit:
                            raise DocumentExtractionError('DOCX展开大小超过限制')
    except DocumentExtractionError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError) as exc:
        raise DocumentExtractionError('DOCX文件损坏或无法读取') from exc
    finally:
        if position is not None:
            source.seek(position)


def validate_document_upload(upload):
    if getattr(upload, 'size', 0) > document_limit('REQUIREMENT_DOCUMENT_MAX_FILE_BYTES'):
        raise DocumentExtractionError('文件大小超过限制（默认10MB）')
    name = str(getattr(upload, 'name', '') or '').lower()
    if not name.endswith(('.pdf', '.docx', '.txt', '.md')):
        raise DocumentExtractionError('仅支持 PDF、DOCX、TXT 或 Markdown 文件')
    if name.endswith('.docx'):
        validate_docx_archive(upload)
    return upload


def validate_document_path(path):
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise DocumentExtractionError('文档文件无法读取') from exc
    if size > document_limit('REQUIREMENT_DOCUMENT_MAX_FILE_BYTES'):
        raise DocumentExtractionError('文件大小超过限制（默认10MB）')


class TextBudget:
    def __init__(self):
        self.limit = document_limit('REQUIREMENT_DOCUMENT_MAX_TEXT_CHARS')
        self.length = 0
        self.parts = []

    def append(self, value):
        if not value:
            return
        self.length += len(value) + bool(self.parts)
        if self.length > self.limit:
            raise DocumentExtractionError('提取文本长度超过限制')
        self.parts.append(value)

    def finish(self, empty_message):
        text = '\n'.join(self.parts).strip()
        if not text:
            raise DocumentExtractionError(empty_message)
        return text
