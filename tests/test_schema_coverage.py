"""Schema generation must retain every previously omitted handler and typed payloads."""
import re

import pytest
from drf_spectacular.drainage import GENERATOR_STATS
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.settings import spectacular_settings
from drf_spectacular.validation import validate_schema


PREVIOUSLY_OMITTED_HANDLERS = {
    'ApiDashboardViewSet', 'AppConfigViewSet', 'AppDashboardViewSet', 'ChatViewSet',
    'get_captcha', 'exchange_token_view', 'login_view', 'logout_view',
    'get_current_user', 'profile_view', 'send_register_code', 'sms_login_view',
    'websocket_ticket_view', 'DocsListView', 'DocsContentView',
    'ExecutionEvidenceExplorerView', 'ResultImportView', 'ExecutionMetricsView',
    'CIWebhookView', 'BatchTemplateDownloadView', 'BatchFileUploadView',
    'JudgeServiceConfigView', 'DashboardStatsView', 'JudgeSingleView',
    'McpConnectionConfigView', 'McpPendingApproveView', 'McpPendingRejectView',
    'McpToolCatalogView', 'McpToolDetailView', 'DashboardViewSet',
    'PerfComparisonReportViewSet', 'EngineStatusView', 'get_project_members',
    'remove_project_member', 'add_project_member', 'get_all_projects',
    'user_projects_list', 'IngestView', 'analyze_text', 'ConfigStatusViewSet',
    'upload_and_analyze', 'TestCaseImportFailureReportDownloadView',
    'TestCaseImportTemplateDownloadView', 'TestCaseSyncProfileSyncView',
    'TestSuiteAddCaseView', 'TestSuiteAddCasesView', 'TestSuiteRemoveCaseView',
    'TestSuiteReorderCasesView', 'TestSuiteCaseListView', 'TestSuiteCaseDetailView',
    'EnvironmentConfigViewSet', 'UiDashboardViewSet', 'get_project_versions',
}


@pytest.fixture(scope='module')
def complete_schema(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        generator = SchemaGenerator()
        schema = generator.get_schema(request=None, public=True)
        errors = dict(GENERATOR_STATS._error_cache)
        endpoints = generator._get_paths_and_endpoints()
    return schema, errors, endpoints


def _payload_is_described(schema, document, seen=None):
    seen = set() if seen is None else seen
    reference = schema.get('$ref')
    if reference:
        if reference in seen:
            return True
        seen.add(reference)
        node = document
        for part in reference.removeprefix('#/').split('/'):
            node = node[part.replace('~1', '/').replace('~0', '~')]
        return _payload_is_described(node, document, seen)
    if schema.get('type') == 'array':
        return _payload_is_described(schema.get('items', {}), document, seen)
    if schema.get('type') == 'object':
        return bool(schema.get('properties')) or 'additionalProperties' in schema
    if schema.get('type') in {'string', 'integer', 'number', 'boolean'}:
        return True
    return any(_payload_is_described(part, document, seen.copy()) for keyword in ('oneOf', 'anyOf', 'allOf') for part in schema.get(keyword, []))


def test_complete_schema_has_no_ignored_view_errors(complete_schema):
    _schema, errors, _endpoints = complete_schema
    assert errors == {}, errors


def test_every_previously_omitted_handler_retains_its_actual_operations(complete_schema):
    schema, _errors, endpoints = complete_schema
    seen = set()
    for path, _regex, method, view in endpoints:
        name = view.__class__.__name__
        if name not in PREVIOUSLY_OMITTED_HANDLERS or method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
            continue
        seen.add(name)
        if spectacular_settings.SCHEMA_PATH_PREFIX_TRIM:
            prefix = spectacular_settings.SCHEMA_PATH_PREFIX or ''
            path = re.sub('^' + prefix.lstrip('^'), '', path, flags=re.IGNORECASE)
        path = spectacular_settings.SCHEMA_PATH_PREFIX_INSERT + path
        if not path.startswith('/'):
            path = '/' + path
        assert path in schema['paths'], (name, method, path)
        assert method.lower() in schema['paths'][path], (name, method, path)
        operation = schema['paths'][path][method.lower()]
        success = {str(code): response for code, response in operation['responses'].items() if str(code).startswith('2')}
        assert success, (name, method, path)
        for code, response in success.items():
            if code == '204':
                continue
            contents = response.get('content', {})
            assert contents, (name, method, path, code)
            assert all(_payload_is_described(media.get('schema', {}), schema) for media in contents.values()), (name, method, path, response)
    assert seen == PREVIOUSLY_OMITTED_HANDLERS, sorted(PREVIOUSLY_OMITTED_HANDLERS - seen)


def test_complete_schema_is_valid_openapi(complete_schema):
    schema, _errors, _endpoints = complete_schema
    validate_schema(schema)
