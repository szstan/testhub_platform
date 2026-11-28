"""Resource-bounded PDF text extraction in an isolated Python process.

This module has no Django imports so the worker can run with ``python -I``.
"""

import contextlib
import json
import os
import subprocess
import sys
import threading
import time


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PAGES = 500
MAX_TEXT_CHARS = 1_000_000
MAX_TIMEOUT_SECONDS = 30
MAX_MEMORY_BYTES = 256 * 1024 * 1024


class PdfWorkerFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def extract_pdf_text(reader_factory, source, max_pages, max_text_chars):
    """Extract bounded text from a reader; kept pure for focused unit tests."""
    reader = reader_factory(source)
    pages = reader.pages
    page_count = len(pages)
    if page_count > max_pages:
        raise PdfWorkerFailure('page_limit')

    parts = []
    total_chars = 0
    for index in range(page_count):
        page = pages[index]
        extract_page_text = getattr(page, 'extract_text', None) or getattr(page, 'extractText', None)
        if extract_page_text is None:
            raise PdfWorkerFailure('invalid_pdf')
        page_text = extract_page_text()
        if page_text is None or page_text == '':
            continue
        if not isinstance(page_text, str):
            raise PdfWorkerFailure('invalid_pdf')
        projected_chars = total_chars + len(page_text) + bool(parts)
        if projected_chars > max_text_chars:
            raise PdfWorkerFailure('text_limit')
        total_chars = projected_chars
        parts.append(page_text)

    text = '\n'.join(parts).strip()
    if not text:
        raise PdfWorkerFailure('empty')
    return text


def _set_posix_limit(resource, kind, requested):
    soft, hard = resource.getrlimit(kind)
    infinity = resource.RLIM_INFINITY
    limits = [requested]
    if soft != infinity:
        limits.append(soft)
    if hard != infinity:
        limits.append(hard)
    effective = min(limits)
    resource.setrlimit(kind, (effective, effective))


def _set_windows_memory_limit(limit_bytes):
    import ctypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ('PerProcessUserTimeLimit', ctypes.c_int64),
            ('PerJobUserTimeLimit', ctypes.c_int64),
            ('LimitFlags', ctypes.c_uint32),
            ('MinimumWorkingSetSize', ctypes.c_size_t),
            ('MaximumWorkingSetSize', ctypes.c_size_t),
            ('ActiveProcessLimit', ctypes.c_uint32),
            ('Affinity', ctypes.c_size_t),
            ('PriorityClass', ctypes.c_uint32),
            ('SchedulingClass', ctypes.c_uint32),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ('ReadOperationCount', ctypes.c_uint64),
            ('WriteOperationCount', ctypes.c_uint64),
            ('OtherOperationCount', ctypes.c_uint64),
            ('ReadTransferCount', ctypes.c_uint64),
            ('WriteTransferCount', ctypes.c_uint64),
            ('OtherTransferCount', ctypes.c_uint64),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ('BasicLimitInformation', BasicLimitInformation),
            ('IoInfo', IoCounters),
            ('ProcessMemoryLimit', ctypes.c_size_t),
            ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t),
            ('PeakJobMemoryUsed', ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle_type = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = handle_type
    kernel32.SetInformationJobObject.argtypes = [
        handle_type, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [handle_type, handle_type]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = handle_type
    kernel32.CloseHandle.argtypes = [handle_type]
    kernel32.CloseHandle.restype = ctypes.c_int

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError('CreateJobObjectW failed')

    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = limit_bytes
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info),
    ):
        kernel32.CloseHandle(job)
        raise OSError('SetInformationJobObject failed')
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        kernel32.CloseHandle(job)
        raise OSError('AssignProcessToJobObject failed')

    _WINDOWS_JOB_HANDLES.append(job)


_WINDOWS_JOB_HANDLES = []


def _install_resource_limits(memory_limit_bytes, cpu_limit_seconds):
    if os.name == 'nt':
        _set_windows_memory_limit(memory_limit_bytes)
        return

    try:
        import resource
        address_space = resource.RLIMIT_AS
        cpu_time = resource.RLIMIT_CPU
    except (ImportError, AttributeError) as exc:
        raise OSError('resource limits are unavailable') from exc

    _set_posix_limit(resource, address_space, memory_limit_bytes)
    _set_posix_limit(resource, cpu_time, cpu_limit_seconds)


def _worker_environment():
    names = (
        ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'PATHEXT')
        if os.name == 'nt'
        else ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH')
    )
    return {name: os.environ[name] for name in names if name in os.environ}


def _positive_int(value, maximum, code='limits'):
    if type(value) is not int or value <= 0:
        raise PdfWorkerFailure(code)
    return min(value, maximum)


def _bounded_limits(max_pages, max_text_chars, max_file_bytes, timeout_seconds, memory_limit_bytes):
    return {
        'max_pages': _positive_int(max_pages, MAX_PAGES),
        'max_text_chars': _positive_int(max_text_chars, MAX_TEXT_CHARS),
        'max_file_bytes': _positive_int(max_file_bytes, MAX_FILE_BYTES),
        'timeout_seconds': _positive_int(timeout_seconds, MAX_TIMEOUT_SECONDS),
        'memory_limit_bytes': _positive_int(memory_limit_bytes, MAX_MEMORY_BYTES),
    }


def _worker_command(file_path, limits):
    return [
        sys.executable,
        '-I',
        os.path.abspath(__file__),
        '--run',
        os.fspath(file_path),
        str(limits['max_pages']),
        str(limits['max_text_chars']),
        str(limits['max_file_bytes']),
        str(limits['memory_limit_bytes']),
        str(limits['timeout_seconds']),
    ]


def _terminate_and_wait(process):
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    process.communicate()


_PDF_ADMISSION = threading.BoundedSemaphore(1)
if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=lambda: _reset_admission_after_fork())


def _reset_admission_after_fork():
    global _PDF_ADMISSION
    _PDF_ADMISSION = threading.BoundedSemaphore(1)


def execute_pdf_worker(
    file_path, *, max_pages, max_text_chars, max_file_bytes,
    timeout_seconds, memory_limit_bytes,
):
    """Run one fixed PDF worker, rejecting concurrent work instead of queueing."""
    try:
        limits = _bounded_limits(
            max_pages, max_text_chars, max_file_bytes, timeout_seconds,
            memory_limit_bytes,
        )
    except PdfWorkerFailure:
        raise

    if not _PDF_ADMISSION.acquire(blocking=False):
        raise PdfWorkerFailure('busy')

    process = None
    deadline = time.monotonic() + limits['timeout_seconds']
    try:
        try:
            process = subprocess.Popen(
                _worker_command(file_path, limits),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_worker_environment(),
                close_fds=True,
            )
        except OSError as exc:
            raise PdfWorkerFailure('worker') from exc

        try:
            stdout, _ = process.communicate(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            _terminate_and_wait(process)
            raise PdfWorkerFailure('timeout') from exc

        max_response_bytes = limits['max_text_chars'] * 6 + 8192
        if len(stdout) > max_response_bytes:
            raise PdfWorkerFailure('worker')
        if process.returncode != 0:
            raise PdfWorkerFailure('resource')
        try:
            result = json.loads(stdout.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PdfWorkerFailure('worker') from None
        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
            raise PdfWorkerFailure('worker')
        if not result['ok']:
            code = result.get('error')
            if not isinstance(code, str) or code not in {
                'dependency', 'limits', 'file_size', 'page_limit', 'text_limit',
                'empty', 'invalid_pdf', 'resource',
            }:
                code = 'worker'
            raise PdfWorkerFailure(code)
        text = result.get('text')
        if not isinstance(text, str) or len(text) > limits['max_text_chars']:
            raise PdfWorkerFailure('worker')
        return text
    finally:
        try:
            if process is not None and process.poll() is None:
                _terminate_and_wait(process)
        finally:
            _PDF_ADMISSION.release()


def _emit_result(result):
    payload = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    sys.stdout.buffer.write(payload.encode('utf-8', errors='replace'))
    sys.stdout.buffer.flush()


def _load_pdf_reader():
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        from PyPDF2 import PdfFileReader as PdfReader
    return PdfReader


def _run_worker(argv):
    if len(argv) != 7 or argv[0] != '--run':
        _emit_result({'ok': False, 'error': 'worker'})
        return 0
    try:
        file_path = argv[1]
        max_pages = _positive_int(int(argv[2]), MAX_PAGES)
        max_text_chars = _positive_int(int(argv[3]), MAX_TEXT_CHARS)
        max_file_bytes = _positive_int(int(argv[4]), MAX_FILE_BYTES)
        memory_limit_bytes = _positive_int(int(argv[5]), MAX_MEMORY_BYTES)
        cpu_limit_seconds = _positive_int(int(argv[6]), MAX_TIMEOUT_SECONDS)
    except (TypeError, ValueError, PdfWorkerFailure):
        _emit_result({'ok': False, 'error': 'limits'})
        return 0

    try:
        _install_resource_limits(memory_limit_bytes, cpu_limit_seconds)
    except BaseException:
        _emit_result({'ok': False, 'error': 'limits'})
        return 0

    try:
        with open(file_path, 'rb') as source:
            if os.fstat(source.fileno()).st_size > max_file_bytes:
                raise PdfWorkerFailure('file_size')
            with open(os.devnull, 'w', encoding='utf-8') as sink:
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    try:
                        reader_factory = _load_pdf_reader()
                    except ImportError:
                        raise PdfWorkerFailure('dependency') from None
                    text = extract_pdf_text(
                        reader_factory, source, max_pages, max_text_chars,
                    )
    except PdfWorkerFailure as exc:
        _emit_result({'ok': False, 'error': exc.code})
        return 0
    except MemoryError:
        _emit_result({'ok': False, 'error': 'resource'})
        return 0
    except BaseException:
        _emit_result({'ok': False, 'error': 'invalid_pdf'})
        return 0

    try:
        _emit_result({'ok': True, 'text': text})
    except MemoryError:
        _emit_result({'ok': False, 'error': 'resource'})
    return 0


if __name__ == '__main__':
    raise SystemExit(_run_worker(sys.argv[1:]))
