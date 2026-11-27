"""Compact execution summaries used by the UI workbench and its data migration."""

import json
import re


INTERNAL_CASE_EXECUTION_ERROR = '用例执行出现内部错误，请联系管理员排查。'
INTERNAL_SUITE_EXECUTION_ERROR = '套件执行出现内部错误，请联系管理员排查。'
EXECUTION_STACKTRACE_MARKERS = (
    'Traceback (most recent call last):',
    'Stacktrace:\n',
    'Stacktrace:\r\n',
    r'Stacktrace:\n',
    r'Stacktrace:\r\n',  # Selenium stack frames inside JSON-encoded step logs.
)
FAILURE_STATUSES = frozenset({'failed', 'error'})
GENERIC_FAILURE_MESSAGE = '执行失败'

_STATUS_ALIASES = {
    'success': 'passed',
    'successful': 'passed',
    'complete': 'passed',
    'completed': 'passed',
    'cancelled': 'aborted',
    'canceled': 'aborted',
    'stopped': 'aborted',
}
EXECUTION_STATUSES = FAILURE_STATUSES | {'pending', 'running', 'passed', 'aborted'}
EXECUTION_STORAGE_STATUSES = EXECUTION_STATUSES | _STATUS_ALIASES.keys()
_STEP_NUMBER = re.compile(r'(?:step|步骤)\s*[#：:]?\s*(\d+)', re.IGNORECASE)


def normalize_execution_status(value):
    status = str(value or '').strip().lower()
    status = _STATUS_ALIASES.get(status, status)
    return status if status in EXECUTION_STATUSES else 'error'


def execution_status_filter_values(value):
    statuses = FAILURE_STATUSES if value == 'failed' else {value}
    return tuple(sorted(raw for raw in EXECUTION_STORAGE_STATUSES if normalize_execution_status(raw) in statuses))


def contains_execution_stacktrace(value):
    return isinstance(value, str) and any(marker in value for marker in EXECUTION_STACKTRACE_MARKERS)


def public_case_diagnostics(logs, error_message):
    if contains_execution_stacktrace(logs):
        steps = _decode_payload(logs)
        if not isinstance(steps, (dict, list)):
            return INTERNAL_CASE_EXECUTION_ERROR, INTERNAL_CASE_EXECUTION_ERROR
        logs = json.dumps(public_execution_evidence(steps), ensure_ascii=False)
        error_message = error_message or INTERNAL_CASE_EXECUTION_ERROR
    return logs, public_execution_evidence(error_message)


def public_suite_error(error_message):
    return INTERNAL_SUITE_EXECUTION_ERROR if contains_execution_stacktrace(error_message) else error_message


def public_execution_evidence(value, internal_error=INTERNAL_CASE_EXECUTION_ERROR):
    """Redact stack-bearing values in JSON results while preserving their structure."""
    if isinstance(value, dict):
        return {key: public_execution_evidence(item, internal_error) for key, item in value.items()}
    if isinstance(value, list):
        return [public_execution_evidence(item, internal_error) for item in value]
    return internal_error if contains_execution_stacktrace(value) else value


def _decode_payload(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return value


def _first_failure(items):
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        success = item.get('success')
        if isinstance(success, bool):
            if not success:
                return item
            continue
        status = str(item.get('status') or '').strip().lower()
        if status in FAILURE_STATUSES or (not status and (item.get('error') or item.get('error_message'))):
            return item
    return None


def _first_message(*values, internal_error):
    for value in values:
        if isinstance(value, str) and value.strip():
            if contains_execution_stacktrace(value):
                return internal_error
            return value.lstrip().split('\n', 1)[0].strip()[:500]
    return ''


def _step_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def build_failure_summary(status, error_message='', payload=None, *, suite=False):
    """Project terminal failure evidence without including runner payloads or screenshots."""
    if normalize_execution_status(status) not in FAILURE_STATUSES:
        return {}
    internal_error = INTERNAL_SUITE_EXECUTION_ERROR if suite else INTERNAL_CASE_EXECUTION_ERROR
    decoded = _decode_payload(payload)
    if not isinstance(decoded, (dict, list)) and (
        contains_execution_stacktrace(payload) or contains_execution_stacktrace(decoded)
    ):
        return {'message': internal_error, 'step_number': None}

    evidence = decoded if isinstance(decoded, dict) else {}
    failed_case = _first_failure(evidence.get('test_cases')) if suite else None
    if isinstance(failed_case, dict):
        steps = failed_case.get('steps')
    else:
        steps = decoded if isinstance(decoded, list) else evidence.get('steps')
    failed_step = _first_failure(steps) or {}
    failed_case = failed_case or {}

    text_log = decoded.rstrip().rsplit('\n', 1)[-1] if isinstance(decoded, str) else ''
    message = _first_message(
        error_message, evidence.get('error_message'), evidence.get('error'), evidence.get('message'),
        failed_case.get('error'), failed_case.get('error_message'), failed_step.get('error'),
        failed_step.get('message'), text_log,
        internal_error=internal_error,
    )
    step_number = _step_number(failed_step.get('step_number')) or _step_number(evidence.get('failed_step_number'))
    if step_number is None:
        match = _STEP_NUMBER.search(message)
        if match:
            step_number = _step_number(match.group(1))
    return {'message': message or GENERIC_FAILURE_MESSAGE, 'step_number': step_number}
