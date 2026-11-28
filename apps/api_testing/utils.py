import json
import re
import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.http_transport import guarded_request
from apps.core.network import UnsafeOutboundURL, validate_outbound_url
from apps.quality_intelligence.services.redaction import (
    REDACTION_MARKER,
    is_sensitive_key,
    redact_payload,
)

from .evidence_redaction import remember_runtime_values, runtime_scrubber
from .models import RequestHistory
from .variable_resolver import VariableResolver


_JSONPATH_KEY_RE = re.compile(r"(?:\.{1,2}([A-Za-z_][A-Za-z0-9_-]*)|\[['\"]([^'\"]+)['\"]\])")
_UNRESOLVED_VARIABLE_RE = re.compile(r'\{\{[^{}]*\}\}|\$\{[^{}]*\}')


def can_access_api_project(project, user, allow_admin=True):
    """Check whether a user can access an API project under the configured policy."""
    if project is None or not user or not getattr(user, 'is_authenticated', False):
        return False
    if allow_admin and (getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False)):
        return True
    return bool(
        project.owner_id == user.pk
        or project.members.filter(pk=user.pk).exists()
    )


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


def redact_execution_evidence(result, scrub):
    """Sanitize evidence fields without changing execution/HTTP verdict metadata."""
    for field in ('request_data', 'response_data', 'error', 'url'):
        if field in result:
            result[field] = redact_payload(scrub(result[field]))
    snapshot = result.get('response_data')
    if isinstance(snapshot, dict) and snapshot.get('json') is not None:
        snapshot['body'] = redact_payload(json.dumps(snapshot['json'], ensure_ascii=False))
    assertions = []
    for assertion in result.get('assertions_results', []):
        assertions.append({
            key: value if key in {'passed', 'type'} else scrub(value)
            for key, value in assertion.items()
        })
    result['assertions_results'] = redact_assertion_results(assertions)
    return result


def _assertion_expected(assertion):
    if assertion.get('type') == 'header':
        return assertion.get('expected', assertion.get('expected_value'))
    return assertion.get('expected', assertion.get('value'))


def _json_values_equal(actual, expected):
    """Compare JSON structures without key-order or bool/number ambiguity."""
    if isinstance(actual, dict) or isinstance(expected, dict):
        return (
            isinstance(actual, dict) and isinstance(expected, dict)
            and actual.keys() == expected.keys()
            and all(_json_values_equal(actual[key], expected[key]) for key in actual)
        )
    if isinstance(actual, list) or isinstance(expected, list):
        return (
            isinstance(actual, list) and isinstance(expected, list)
            and len(actual) == len(expected)
            and all(_json_values_equal(left, right) for left, right in zip(actual, expected))
        )
    if actual is None or expected is None:
        return actual is expected
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def execute_assertions(response, assertions, *, defer_redaction=False):
    """Evaluate raw values; case runners may defer evidence until extraction ends."""
    results = []
    json_loaded = False
    response_json = safe_response_json = None
    json_error = None

    for assertion in assertions:
        assertion_type = assertion.get('type')
        expected = _assertion_expected(assertion)
        result = {
            'name': assertion.get('name', '未命名断言'),
            'type': assertion_type,
            'passed': False,
            'expected': expected,
            'actual': None,
            'error': None,
        }
        if assertion_type == 'json_path':
            result['json_path'] = assertion.get('json_path', '')
        elif assertion_type == 'header':
            result['header_name'] = assertion.get('header_name', '')

        try:
            actual = None
            passed = False

            if assertion_type == 'status_code':
                actual = response.status_code
                passed = actual == expected
            elif assertion_type == 'response_time':
                actual = assertion.get('actual_time')
                passed = actual <= expected if actual is not None else False
            elif assertion_type == 'contains':
                text = response.text or ''
                pattern = str(expected)
                actual = text if defer_redaction else text[:200] + '...' if len(text) > 200 else text
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
                        safe_response_json = None if defer_redaction else redact_payload(response_json)
                    except (TypeError, ValueError) as exc:
                        json_error = str(exc)
                if json_error is not None:
                    raise ValueError(json_error)
                from jsonpath_ng import parse

                expression = parse(json_path)
                matches = expression.find(response_json)
                actual = matches[0].value if matches else None
                # The interface editor historically stored scalar expectations as
                # strings. Keep that compatibility while comparing native JSON
                # values and containers without Python string representations.
                if (
                    assertion.get('comparison') != 'json'
                    and isinstance(expected, str)
                    and not isinstance(actual, (dict, list))
                ):
                    passed = bool(matches) and str(actual) == expected
                else:
                    passed = bool(matches) and _json_values_equal(actual, expected)
                if not matches:
                    result['error'] = 'JSON路径未匹配任何值'
                if not defer_redaction:
                    redacted_matches = expression.find(safe_response_json)
                    result['actual'] = redacted_matches[0].value if redacted_matches else actual
            elif assertion_type == 'header':
                header_name = assertion.get('header_name', '')
                actual = response.headers.get(header_name)
                passed = actual == expected
            elif assertion_type == 'equals':
                actual = (response.text or '').strip()
                passed = actual == str(expected).strip()

            if result['actual'] is None:
                result['actual'] = actual
            result['passed'] = passed
        except Exception as exc:
            result['error'] = str(exc) if defer_redaction else redact_payload(str(exc))
            result['passed'] = False

        results.append(result if defer_redaction else redact_assertion_results([result])[0])

    return results


def _response_json(response, *, defer_redaction=False):
    if not response.headers.get('content-type', '').lower().startswith('application/json'):
        return None
    try:
        payload = json.loads(response.text)
        return payload if defer_redaction else redact_payload(payload)
    except (TypeError, ValueError):
        return None


def redact_response_data(response, *, defer_redaction=False):
    safe_json = _response_json(response, defer_redaction=defer_redaction)
    body = (
        json.dumps(safe_json, ensure_ascii=False)
        if safe_json is not None
        else response.text if defer_redaction else redact_payload(response.text)
    )
    return {
        'headers': dict(response.headers) if defer_redaction else redact_payload(dict(response.headers)),
        'body': body,
        'json': safe_json,
    }


def _resolve_request_value(value, resolver, variables):
    if isinstance(value, dict):
        return {
            key: _resolve_request_value(item, resolver, variables)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_request_value(item, resolver, variables) for item in value]
    if isinstance(value, str):
        return resolver.resolve(_replace_variables(value, variables))
    return value


def _unresolved_variables(value):
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, list):
        values = value
    elif isinstance(value, tuple):
        values = value
    elif isinstance(value, str):
        return _UNRESOLVED_VARIABLE_RE.findall(value)
    else:
        return []
    return [name for item in values for name in _unresolved_variables(item)]


def _resolved_request(
    api_request, environment, variable_overrides=None, request_overrides=None, runtime_value_sink=None,
):
    variables = dict(environment.variables) if environment else {}
    variables.update(variable_overrides or {})
    resolver = VariableResolver(
        on_value=lambda value: remember_runtime_values(value, runtime_value_sink)
        if runtime_value_sink is not None else None,
    )
    overrides = request_overrides or {}

    url = _resolve_request_value(api_request.url, resolver, variables)
    if isinstance(api_request.headers, list):
        headers = {
            item['key']: item.get('value', '')
            for item in api_request.headers
            if item.get('enabled', True) and item.get('key')
        }
    else:
        headers = dict(api_request.headers or {})
    headers.update(overrides.get('headers') or {})
    headers = _resolve_request_value(headers, resolver, variables)

    params = dict(api_request.params or {})
    params.update(overrides.get('params') or {})
    params = _resolve_request_value(params, resolver, variables)

    body = overrides.get('body', api_request.body or {})
    if body in ({}, None):
        body = {'type': 'none', 'data': None}
    elif not isinstance(body, dict) or 'type' not in body:
        body = {'type': 'json', 'data': body}
    body_type = body.get('type', 'none')
    body_data = body.get('data')
    if api_request.method not in {'POST', 'PUT', 'PATCH'}:
        body_type, body_data = 'none', None
    elif body_type != 'none':
        body_data = _resolve_request_value(body_data, resolver, variables)

    unresolved = _unresolved_variables((url, headers, params, body_data))
    if unresolved:
        raise ValueError('请求中存在未解析变量，已阻止发送')
    return url, headers, params, body_type, body_data


def _form_fields(body_data):
    if isinstance(body_data, dict):
        return list(body_data.items())
    if not isinstance(body_data, list):
        raise ValueError('表单请求体必须是对象或字段数组')
    fields = []
    for item in body_data:
        if not isinstance(item, dict) or item.get('enabled', True) is False:
            continue
        key = item.get('key') or item.get('name')
        if key:
            fields.append((key, item.get('value', '')))
    return fields


def execute_test_suite(test_suite, environment, executed_by, *, cancel_check=None, variable_overrides=None):
    """Execute mixed request and API-case members in stable suite order."""
    from .case_runner import run_api_test_case
    from .models import (
        ApiCollection, ApiRequest, ApiTestCase, TestExecution, TestSuite,
        TestSuiteCase, TestSuiteRequest,
    )

    execution = None
    results = []
    passed_count = failed_count = 0
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
        with transaction.atomic():
            initial_project_id = test_suite.project_id
            locked_suite = TestSuite.objects.select_for_update().filter(pk=test_suite.pk).first()
            if locked_suite is None:
                return {'success': False, 'error': '测试套件不存在'}
            if locked_suite.project_id != initial_project_id:
                return {'success': False, 'error': '测试套件所属项目已变更，请重新选择'}
            test_suite = locked_suite
            if not can_access_api_project(test_suite.project, executed_by):
                return {'success': False, 'error': '无权访问该测试套件'}
            if not can_use_environment(environment, executed_by, project=test_suite.project):
                return {'success': False, 'error': '无权使用该执行环境'}

            request_members = list(
                TestSuiteRequest.objects.select_for_update()
                .filter(test_suite=test_suite, enabled=True)
                .order_by('order', 'pk')
            )
            case_members = list(
                TestSuiteCase.objects.select_for_update()
                .filter(test_suite=test_suite, enabled=True)
                .select_related('test_case')
                .order_by('order', 'pk')
            )
            request_ids = {item.request_id for item in request_members}
            request_locations = dict(
                ApiRequest.objects.filter(pk__in=request_ids).values_list('pk', 'collection_id')
            )
            collection_ids = sorted({pk for pk in request_locations.values() if pk is not None})
            collections = {
                item.pk: item for item in ApiCollection.objects.select_for_update()
                .filter(pk__in=collection_ids).order_by('pk')
            }
            requests = {
                item.pk: item for item in ApiRequest.objects.select_for_update()
                .filter(pk__in=request_ids).order_by('pk')
            }
            cases = {
                item.pk: item for item in ApiTestCase.objects.select_for_update()
                .filter(pk__in=[member.test_case_id for member in case_members]).order_by('pk')
            }
            for member in request_members:
                api_request = requests.get(member.request_id)
                collection = collections.get(request_locations.get(member.request_id))
                if (
                    api_request is None or collection is None
                    or collection.project_id != test_suite.project_id
                    or api_request.collection_id != collection.pk
                ):
                    raise ValueError('测试套件中的请求已不属于当前项目')
            for member in case_members:
                case = cases.get(member.test_case_id)
                if case is None or case.project_id != test_suite.project_id:
                    raise ValueError('测试套件中的接口用例已不属于当前项目')
                member.test_case = case
            for member in request_members:
                member.request = requests[member.request_id]

            entries = [
                (member.order, 0, member.pk, 'request', member)
                for member in request_members
            ] + [
                (member.order, 1, member.pk, 'case', member)
                for member in case_members
            ]
            entries.sort(key=lambda item: item[:3])
            execution = TestExecution.objects.create(
                test_suite=test_suite,
                status='RUNNING',
                start_time=timezone.now(),
                executed_by=executed_by,
            )
            execution.total_requests = len(entries)
            execution.save(update_fields=['total_requests'])

        for _, _, _, kind, member in entries:
            check_cancelled()
            if kind == 'case':
                case_result = run_api_test_case(
                    member.test_case,
                    environment,
                    executed_by,
                    variable_overrides=variable_overrides,
                    cancel_check=check_cancelled,
                )
                passed = bool(case_result.get('success'))
                if passed:
                    passed_count += 1
                else:
                    failed_count += 1
                results.append({
                    'kind': 'case',
                    'name': member.test_case.name,
                    'passed': passed,
                    'error': redact_payload(case_result.get('error', '')),
                    'passed_steps': case_result.get('passed_count', 0),
                    'failed_steps': case_result.get('failed_count', 0),
                    'skipped_steps': case_result.get('skipped_count', 0),
                    'results': case_result.get('results', []),
                })
                continue

            api_request = member.request
            suite_literals = set()
            for source in (getattr(environment, 'variables', None), variable_overrides):
                remember_runtime_values(source, suite_literals)
            api_result = execute_api_request(
                api_request,
                environment,
                executed_by,
                cancel_check=check_cancelled,
                variable_overrides=variable_overrides,
                runtime_value_sink=suite_literals,
                include_raw_response=True,
            )
            raw_response = api_result.pop('_raw_response', None)
            assertions_results = list(api_result.get('assertions_results') or [])
            if raw_response is not None and member.assertions:
                suite_assertions = [dict(assertion) for assertion in member.assertions]
                for assertion in suite_assertions:
                    if assertion.get('type') == 'response_time':
                        assertion['actual_time'] = api_result.get('response_time')
                assertions_results.extend(execute_assertions(raw_response, suite_assertions, defer_redaction=True))
            passed = bool(api_result.get('success')) and not any(
                not item.get('passed', True) for item in assertions_results
            )
            error_message = api_result.get('error', '')
            if passed is False and not error_message:
                failed_assertion = next(
                    (item for item in assertions_results if not item.get('passed', True)), None,
                )
                if failed_assertion:
                    assertion_name = failed_assertion.get('name') or '未命名断言'
                    assertion_error = failed_assertion.get('error') or '期望值与实际值不一致'
                    error_message = f"断言失败: {assertion_name} - {assertion_error}"
            if passed:
                passed_count += 1
            else:
                failed_count += 1
            results.append(redact_execution_evidence({
                'kind': 'request',
                'name': api_request.name,
                'method': api_request.method,
                'url': api_result.get('url', api_request.url),
                'status_code': api_result.get('status_code'),
                'response_time': api_result.get('response_time'),
                'passed': passed,
                'error': error_message,
                'assertions_results': assertions_results,
            }, runtime_scrubber(suite_literals)))

        execution.end_time = timezone.now()
        execution.passed_requests = passed_count
        execution.failed_requests = failed_count
        execution.status = 'COMPLETED' if failed_count == 0 else 'FAILED'
        execution.results = results
        execution.save()
        return {
            'success': failed_count == 0,
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


def execute_api_request(
    api_request,
    environment,
    executed_by,
    *,
    cancel_check=None,
    variable_overrides=None,
    request_overrides=None,
    assertions_override=None,
    include_raw_response=False,
    record_history=True,
    defer_evidence_redaction=False,
    runtime_value_sink=None,
):
    """Execute one guarded HTTP request with optional ephemeral overrides.

    Deferral is restricted to in-memory case execution. The case runner must
    scrub all discovered runtime values before returning or storing evidence.
    """
    if defer_evidence_redaction and (record_history or not include_raw_response):
        raise ValueError('Deferred evidence is only allowed for in-memory case execution')
    if cancel_check is not None:
        cancel_check()
    if not can_use_environment(environment, executed_by, project=_request_project(api_request)):
        return {'success': False, 'error': '无权使用该执行环境'}
    runtime_literals = runtime_value_sink if runtime_value_sink is not None else set()
    for source in (getattr(environment, 'variables', None), variable_overrides):
        remember_runtime_values(source, runtime_literals)

    try:
        url, headers, params, body_type, body_data = _resolved_request(
            api_request, environment, variable_overrides, request_overrides,
            runtime_value_sink=runtime_literals,
        )
        scrub = runtime_scrubber(runtime_literals)
        try:
            url = validate_outbound_url(
                url,
                allow_private=bool(getattr(settings, 'PERF_ALLOW_PRIVATE_TARGETS', False)),
            )
        except UnsafeOutboundURL:
            return {'success': False, 'error': '请求目标地址不允许访问'}

        request_kwargs = {}
        if body_type == 'form-data':
            headers = {key: value for key, value in headers.items() if str(key).casefold() != 'content-type'}
        elif body_type == 'x-www-form-urlencoded':
            content_type_key = next(
                (key for key in headers if str(key).casefold() == 'content-type'),
                'Content-Type',
            )
            headers[content_type_key] = 'application/x-www-form-urlencoded'
        if body_type == 'json':
            request_kwargs['json'] = body_data
        elif body_type == 'raw':
            request_kwargs['data'] = body_data
        elif body_type == 'x-www-form-urlencoded':
            request_kwargs['data'] = _form_fields(body_data or {})
        elif body_type == 'form-data':
            request_kwargs['files'] = [
                (key, (None, str(value))) for key, value in _form_fields(body_data or {})
            ]

        started = time.monotonic()
        response = guarded_request(
            method=api_request.method,
            url=url,
            allow_private=bool(getattr(settings, 'PERF_ALLOW_PRIVATE_TARGETS', False)),
            headers=headers,
            params=params,
            timeout=30,
            allow_redirects=False,
            **request_kwargs,
        )
        response_time = (time.monotonic() - started) * 1000

        assertions = assertions_override
        if assertions is None:
            assertions = api_request.assertions or []
        assertions = [dict(assertion) for assertion in assertions]
        for assertion in assertions:
            if assertion.get('type') == 'response_time':
                assertion['actual_time'] = response_time
        defer_for_values = defer_evidence_redaction or bool(runtime_literals)
        assertions_results = execute_assertions(response, assertions, defer_redaction=defer_for_values)
        safe_response = redact_response_data(response, defer_redaction=defer_for_values)
        evidence = {
            'url': url,
            'request_data': {
                'url': url, 'method': api_request.method, 'headers': headers,
                'params': params, 'body': body_data, 'body_type': body_type,
            },
            'response_data': safe_response,
            'assertions_results': assertions_results,
        }
        if not defer_evidence_redaction:
            redact_execution_evidence(evidence, scrub)
        history = None
        if record_history:
            history = RequestHistory.objects.create(
                request=api_request,
                environment=environment,
                request_data=evidence['request_data'],
                response_data=evidence['response_data'],
                status_code=response.status_code,
                response_time=response_time,
                assertions_results=evidence['assertions_results'],
                executed_by=executed_by,
            )
        result = {
            'success': True,
            'history_id': history.id if history else None,
            'url': evidence['url'],
            'status_code': response.status_code,
            'response_time': response_time,
            'assertions_results': evidence['assertions_results'],
            'response_data': evidence['response_data'],
        }
        if include_raw_response:
            result['_raw_response'] = response
        return result
    except Exception as exc:
        return {
            'success': False,
            'error': str(exc) if defer_evidence_redaction else redact_payload(runtime_scrubber(runtime_literals)(str(exc))),
        }




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
