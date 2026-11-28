"""Multi-step API case execution using the shared guarded request path."""

from __future__ import annotations

import json

from django.db import transaction

from .evidence_redaction import remember_runtime_values, runtime_scrubber
from .utils import (
    can_access_api_project, can_use_environment, execute_api_request, redact_execution_evidence,
)


def _merge_variables(*sources):
    merged = {}
    for source in sources:
        if isinstance(source, dict):
            merged.update(source)
    return merged


def _redact_case_results(results, literals):
    # Include every observed value, even values overwritten by later steps.
    scrub = runtime_scrubber(literals)
    return [redact_execution_evidence(result, scrub) for result in results]


def _extract_value(extractor, raw_response):
    source = extractor['source']
    expression = extractor['expression']
    headers = raw_response.get('headers', {}) if isinstance(raw_response, dict) else raw_response.headers
    body = raw_response.get('body', '') if isinstance(raw_response, dict) else raw_response.text
    if source == 'header':
        target = expression.casefold()
        return next(
            (value for key, value in headers.items() if key.casefold() == target),
            None,
        )

    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    from jsonpath_ng import parse

    matches = parse(expression).find(payload)
    return matches[0].value if matches else None


def run_api_test_case(
    test_case, environment, executed_by, *, variable_overrides=None, cancel_check=None, heartbeat=None,
    frozen_inputs=None,
):
    """Run a case in memory; callers own persistence of its execution record."""
    if frozen_inputs is None:
        from .models import ApiTestCase

        with transaction.atomic():
            initial_project_id = test_case.project_id
            locked_case = ApiTestCase.objects.select_for_update().filter(pk=test_case.pk).first()
            if locked_case is None:
                return {'success': False, 'error': '接口测试用例不存在'}
            if locked_case.project_id != initial_project_id:
                return {'success': False, 'error': '接口测试用例所属项目已变更'}
            if not can_access_api_project(locked_case.project, executed_by):
                return {'success': False, 'error': '无权访问该接口测试用例'}
            if not can_use_environment(environment, executed_by, project=locked_case.project):
                return {'success': False, 'error': '无权使用该执行环境'}

            steps = list(locked_case.steps.select_related('request__collection').order_by('order', 'pk'))
            if len(steps) > 50:
                return {'success': False, 'error': '单个用例最多包含50个步骤'}
            case_variables = dict(locked_case.variables or {})
            stop_on_failure = locked_case.stop_on_failure
            project_id = locked_case.project_id
    else:
        locked_case = frozen_inputs.test_case
        environment = frozen_inputs.environment
        steps = list(frozen_inputs.steps)
        if len(steps) > 50:
            return {'success': False, 'error': '单个用例最多包含50个步骤'}
        if not can_access_api_project(locked_case.project, executed_by):
            return {'success': False, 'error': '无权访问该接口测试用例'}
        if not can_use_environment(environment, executed_by, project=locked_case.project):
            return {'success': False, 'error': '无权使用该执行环境'}
        case_variables = dict(locked_case.variables or {})
        stop_on_failure = locked_case.stop_on_failure
        project_id = locked_case.project_id

    variables = _merge_variables(
        getattr(environment, 'variables', None),
        case_variables,
        variable_overrides,
    )
    runtime_literals = set()
    for source in (getattr(environment, 'variables', None), case_variables, variable_overrides):
        remember_runtime_values(source, runtime_literals)
    results = []
    passed_count = failed_count = skipped_count = 0
    halted = False

    for step in steps:
        if heartbeat is not None:
            heartbeat()
        if cancel_check is not None:
            cancel_check()
        if not step.enabled or halted:
            skipped_count += 1
            results.append({
                'name': step.name,
                'order': step.order,
                'status': 'skipped',
                'passed': None,
                'error': '',
                'assertions_results': [],
            })
            continue

        api_request = step.request
        if (
            api_request.request_type != 'HTTP'
            or api_request.collection_id is None
            or api_request.collection.project_id != project_id
        ):
            step_result = {
                'success': False,
                'error': '步骤请求不再属于当前用例项目',
            }
        else:
            step_result = execute_api_request(
                api_request,
                environment,
                executed_by,
                cancel_check=cancel_check,
                variable_overrides=variables,
                request_overrides=step.request_overrides,
                assertions_override=step.assertions,
                include_raw_response=True,
                record_history=False,
                defer_evidence_redaction=True,
                runtime_value_sink=runtime_literals,
            )

        raw_response = step_result.pop('_raw_response', None)
        assertions = step_result.get('assertions_results') or []
        passed = bool(step_result.get('success')) and not any(
            not assertion.get('passed', True) for assertion in assertions
        )
        error = step_result.get('error', '')
        extracted_names = []

        if passed and raw_response is not None:
            for extractor in step.extractors or []:
                try:
                    value = _extract_value(extractor, raw_response)
                except Exception as exc:  # invalid JSONPath fails the step, not the worker
                    passed = False
                    error = f"变量提取失败: {extractor.get('name', '')} - {exc}"
                    break
                if value is None:
                    if extractor.get('required', False):
                        passed = False
                        if not error:
                            error = f"缺少必需提取变量: {extractor.get('name', '')}"
                        break
                    continue
                remember_runtime_values(value, runtime_literals)
                variables[extractor['name']] = value
                extracted_names.append(extractor['name'])

        if passed:
            passed_count += 1
            status = 'passed'
        else:
            failed_count += 1
            status = 'failed'
            if stop_on_failure:
                halted = True

        results.append({
            'name': step.name,
            'order': step.order,
            'status': status,
            'method': api_request.method,
            'url': step_result.get('url', api_request.url),
            'status_code': step_result.get('status_code'),
            'response_time': step_result.get('response_time'),
            'passed': passed,
            'error': error,
            'assertions_results': assertions,
            'response_data': step_result.get('response_data'),
            'extracted_variables': extracted_names,
        })

    has_enabled_steps = any(step.enabled for step in steps)
    return {
        'success': has_enabled_steps and failed_count == 0,
        'error': '' if has_enabled_steps else '用例至少需要一个启用的 HTTP 步骤',
        'passed_count': passed_count,
        'failed_count': failed_count,
        'skipped_count': skipped_count,
        'total_count': len(steps),
        'results': _redact_case_results(results, runtime_literals),
    }


# Public name used by suite execution and async case execution.
execute_test_case = run_api_test_case
