import json
import re
import time

from django.conf import settings
from django.utils import timezone

from apps.core.http_transport import guarded_request
from apps.core.network import UnsafeOutboundURL, validate_outbound_url
from apps.quality_intelligence.services.redaction import (
    REDACTION_MARKER,
    is_sensitive_key,
    redact_payload,
)

from .models import RequestHistory
from .variable_resolver import VariableResolver


_JSONPATH_KEY_RE = re.compile(r"(?:\.{1,2}([A-Za-z_][A-Za-z0-9_-]*)|\[['\"]([^'\"]+)['\"]\])")


def can_use_environment(environment, user, project=None):
    """Check environment ownership and, for LOCAL values, project membership."""
    if environment is None:
        return True
    is_admin = bool(getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))
    if environment.scope == 'GLOBAL':
        return is_admin or bool(user and environment.created_by_id == user.pk)
    if environment.scope != 'LOCAL' or not environment.project_id:
        return False
    if project is not None and environment.project_id != project.pk:
        return False
    if is_admin:
        return True
    related_project = environment.project
    return bool(
        user
        and (
            related_project.owner_id == user.pk
            or related_project.members.filter(pk=user.pk).exists()
        )
    )


def _request_project(api_request):
    return getattr(getattr(api_request, 'collection', None), 'project', None)


def _sensitive_json_path(path):
    path = str(path or '')
    if '*' in path or '..' in path:
        return True
    for dot_key, bracket_key in _JSONPATH_KEY_RE.findall(path):
        if is_sensitive_key(dot_key or bracket_key):
            return True
    return False


def redact_assertion_results(results):
    """Redact stored assertion values, including legacy JSONPath records."""
    safe_results = redact_payload(results)
    if not isinstance(results, list) or not isinstance(safe_results, list):
        return safe_results
    for raw, safe in zip(results, safe_results):
        if not isinstance(raw, dict) or not isinstance(safe, dict):
            continue
        assertion_type = raw.get('type')
        sensitive = (
            assertion_type == 'json_path' and _sensitive_json_path(raw.get('json_path'))
        ) or (
            assertion_type == 'header' and is_sensitive_key(raw.get('header_name'))
        )
        if sensitive:
            for field in ('actual', 'expected'):
                if raw.get(field) is not None:
                    safe[field] = REDACTION_MARKER
        for field in ('actual', 'expected', 'error'):
            if field in safe:
                safe[field] = redact_payload(safe[field])
    return safe_results


def execute_assertions(response, assertions):
    """Evaluate assertions against raw response values and return redacted evidence."""
    results = []
    json_loaded = False
    response_json = safe_response_json = None
    json_error = None

    for assertion in assertions:
        assertion_type = assertion.get('type')
        result = {
            'name': assertion.get('name', '未命名断言'),
            'type': assertion_type,
            'passed': False,
            'expected': assertion.get('expected'),
            'actual': None,
            'error': None,
        }
        if assertion_type == 'json_path':
            result['json_path'] = assertion.get('json_path', '')
        elif assertion_type == 'header':
            result['header_name'] = assertion.get('header_name', '')

        try:
            expected = assertion.get('expected')
            actual = None
            passed = False

            if assertion_type == 'status_code':
                actual = response.status_code
                passed = actual == expected
            elif assertion_type == 'response_time':
                actual = assertion.get('actual_time')
                passed = actual <= expected if actual else False
            elif assertion_type == 'contains':
                text = response.text or ''
                pattern = str(expected)
                actual = text[:200] + '...' if len(text) > 200 else text
                passed = pattern in str(text)
            elif assertion_type == 'json_path':
                json_path = assertion.get('json_path', '')
                content_type = response.headers.get('content-type', '').lower()
                if 'application/json' not in content_type:
                    raise ValueError(f"响应不是JSON格式，Content-Type: {content_type}")
                if not json_path:
                    raise ValueError('JSON路径表达式不能为空')

                if not json_loaded:
                    json_loaded = True
                    try:
                        response_json = json.loads(response.text)
                        safe_response_json = redact_payload(response_json)
                    except (TypeError, ValueError) as exc:
                        json_error = str(exc)
                if json_error is not None:
                    raise ValueError(json_error)
                from jsonpath_ng import parse

                expression = parse(json_path)
                matches = expression.find(response_json)
                actual = matches[0].value if matches else None
                passed = str(actual) == str(expected)
                redacted_matches = expression.find(safe_response_json)
                result['actual'] = redacted_matches[0].value if redacted_matches else actual
            elif assertion_type == 'header':
                header_name = assertion.get('header_name', '')
                actual = response.headers.get(header_name)
                passed = actual == assertion.get('expected_value')
                result['expected'] = assertion.get('expected_value')
            elif assertion_type == 'equals':
                actual = (response.text or '').strip()
                passed = actual == str(expected).strip()

            if result['actual'] is None:
                result['actual'] = actual
            result['passed'] = passed
        except Exception as exc:
            result['error'] = redact_payload(str(exc))
            result['passed'] = False

        results.append(redact_assertion_results([result])[0])

    return results


def _response_json(response):
    if not response.headers.get('content-type', '').lower().startswith('application/json'):
        return None
    try:
        return redact_payload(json.loads(response.text))
    except (TypeError, ValueError):
        return None


def redact_response_data(response):
    safe_json = _response_json(response)
    body = (
        json.dumps(safe_json, ensure_ascii=False)
        if safe_json is not None
        else redact_payload(response.text)
    )
    return {
        'headers': redact_payload(dict(response.headers)),
        'body': body,
        'json': safe_json,
    }


def _resolved_request(api_request, environment, variable_overrides=None):
    variables = dict(environment.variables) if environment else {}
    variables.update(variable_overrides or {})
    resolver = VariableResolver()

    url = resolver.resolve(_replace_variables(api_request.url, variables))
    if isinstance(api_request.headers, list):
        headers = {}
        for item in api_request.headers:
            if item.get('enabled', True) and item.get('key'):
                value = _replace_variables(str(item.get('value', '')), variables)
                headers[item['key']] = resolver.resolve(value)
    else:
        headers = dict(api_request.headers or {})
        headers = {
            key: resolver.resolve(_replace_variables(str(value), variables))
            for key, value in headers.items()
        }

    params = dict(api_request.params or {})
    params = {
        key: resolver.resolve(_replace_variables(str(value), variables))
        for key, value in params.items()
    }
    body_data = None
    if api_request.body and api_request.method in {'POST', 'PUT', 'PATCH'}:
        if api_request.body.get('type') == 'json':
            body_data = _replace_variables_in_dict(api_request.body.get('data', {}), variables)
            body_data = _resolve_variables_in_dict(body_data, resolver)
    return url, headers, params, body_data


def execute_test_suite(test_suite, environment, executed_by, *, cancel_check=None, variable_overrides=None):
    """Execute enabled suite requests and forward cancellation to each request."""
    from .models import TestExecution

    execution = None
    results = []
    passed_count = 0
    failed_count = 0
    cancel_error = None

    def check_cancelled():
        nonlocal cancel_error
        if cancel_check is None:
            return
        try:
            cancel_check()
        except Exception as exc:
            cancel_error = exc
            raise

    try:
        check_cancelled()
        if not can_use_environment(environment, executed_by, project=test_suite.project):
            return {'success': False, 'error': '无权使用该执行环境'}

        execution = TestExecution.objects.create(
            test_suite=test_suite,
            status='RUNNING',
            start_time=timezone.now(),
            executed_by=executed_by,
        )
        suite_requests = test_suite.testsuiterequest_set.filter(enabled=True).order_by('order')
        execution.total_requests = suite_requests.count()
        execution.save()

        for suite_request in suite_requests:
            check_cancelled()
            api_request = suite_request.request
            api_result = execute_api_request(
                api_request,
                environment,
                executed_by,
                cancel_check=check_cancelled,
                variable_overrides=variable_overrides,
            )
            assertions_results = api_result.get('assertions_results') or []
            passed = bool(api_result.get('success'))
            error_message = api_result.get('error', '')
            status_code = api_result.get('status_code')

            if passed:
                for assertion in suite_request.assertions or []:
                    if assertion.get('type') == 'status_code' and status_code != assertion.get('value'):
                        passed = False
                        error_message = f"状态码断言失败: 期望 {assertion.get('value')}, 实际 {status_code}"
                        break
            if passed:
                failed_assertion = next(
                    (item for item in assertions_results if not item.get('passed', True)),
                    None,
                )
                if failed_assertion:
                    passed = False
                    error_message = (
                        f"断言失败: {failed_assertion.get('name', '未命名断言')} - "
                        f"{failed_assertion.get('error', '断言不通过')}"
                    )

            if passed:
                passed_count += 1
            else:
                failed_count += 1
            results.append({
                'name': api_request.name,
                'method': api_request.method,
                'url': api_result.get('url', redact_payload(api_request.url)),
                'status_code': status_code,
                'response_time': api_result.get('response_time'),
                'passed': passed,
                'error': redact_payload(error_message),
                'assertions_results': assertions_results,
            })

        execution.end_time = timezone.now()
        execution.passed_requests = passed_count
        execution.failed_requests = failed_count
        execution.status = 'COMPLETED' if failed_count == 0 else 'FAILED'
        execution.results = results
        execution.save()
        return {
            'success': True,
            'execution_id': execution.id,
            'passed_count': passed_count,
            'failed_count': failed_count,
            'total_count': execution.total_requests,
            'results': results,
        }
    except Exception as exc:
        if execution is not None:
            execution.end_time = timezone.now()
            execution.passed_requests = passed_count
            execution.failed_requests = failed_count + 1
            execution.status = 'FAILED'
            execution.results = results
            execution.save()
        if cancel_error is not None:
            raise cancel_error
        return {'success': False, 'error': redact_payload(str(exc))}


def execute_api_request(api_request, environment, executed_by, *, cancel_check=None, variable_overrides=None):
    """Execute one HTTP request; optional cooperative cancellation is additive."""
    if cancel_check is not None:
        cancel_check()
    if not can_use_environment(environment, executed_by, project=_request_project(api_request)):
        return {'success': False, 'error': '无权使用该执行环境'}

    try:
        url, headers, params, body_data = _resolved_request(api_request, environment, variable_overrides)
        try:
            url = validate_outbound_url(
                url,
                allow_private=bool(getattr(settings, 'PERF_ALLOW_PRIVATE_TARGETS', False)),
            )
        except UnsafeOutboundURL:
            return {'success': False, 'error': '请求目标地址不允许访问'}

        started = time.monotonic()
        import requests

        response = guarded_request(
            method=api_request.method,
            url=url,
            allow_private=bool(getattr(settings, 'PERF_ALLOW_PRIVATE_TARGETS', False)),
            headers=headers,
            params=params,
            json=body_data,
            timeout=30,
            allow_redirects=False,
        )
        response_time = (time.monotonic() - started) * 1000

        assertions = [dict(assertion) for assertion in (api_request.assertions or [])]
        for assertion in assertions:
            if assertion.get('type') == 'response_time':
                assertion['actual_time'] = response_time
        assertions_results = execute_assertions(response, assertions)
        safe_response = redact_response_data(response)
        history = RequestHistory.objects.create(
            request=api_request,
            environment=environment,
            request_data={
                'url': redact_payload(url),
                'method': api_request.method,
                'headers': redact_payload(headers),
                'params': redact_payload(params),
                'body': redact_payload(body_data),
            },
            response_data=safe_response,
            status_code=response.status_code,
            response_time=response_time,
            assertions_results=redact_assertion_results(assertions_results),
            executed_by=executed_by,
        )
        return {
            'success': True,
            'history_id': history.id,
            'url': redact_payload(url),
            'status_code': response.status_code,
            'response_time': response_time,
            'assertions_results': redact_assertion_results(assertions_results),
            'response_data': safe_response,
        }
    except Exception as exc:
        return {'success': False, 'error': redact_payload(str(exc))}


def _replace_variables(text, variables):
    """替换文本中的变量"""
    if not isinstance(text, str):
        return text
    result = text
    for key, value in (variables or {}).items():
        if isinstance(value, dict):
            replacement = str(value.get('currentValue', '') or value.get('initialValue', ''))
        else:
            replacement = str(value) if value is not None else ''
        result = result.replace(f'{{{{{key}}}}}', replacement)
    return result


def _replace_variables_in_dict(data, variables):
    """递归替换字典中的变量"""
    if isinstance(data, dict):
        return {key: _replace_variables_in_dict(value, variables) for key, value in data.items()}
    if isinstance(data, list):
        return [_replace_variables_in_dict(item, variables) for item in data]
    if isinstance(data, str):
        return _replace_variables(data, variables)
    return data


def _resolve_variables_in_dict(data, resolver):
    """递归解析字典中的动态函数占位符"""
    if isinstance(data, dict):
        return {key: _resolve_variables_in_dict(value, resolver) for key, value in data.items()}
    if isinstance(data, list):
        return [_resolve_variables_in_dict(item, resolver) for item in data]
    if isinstance(data, str):
        return resolver.resolve(data)
    return data
