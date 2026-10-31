import pytest
from django.urls import include, path
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema

from apps.requirement_analysis.urls import urlpatterns as requirement_urlpatterns
from apps.testcases.urls import urlpatterns as testcase_urlpatterns
from apps.testsuites.urls import urlpatterns as testsuite_urlpatterns


SCHEMA_PATTERNS = [
    path('api/requirement-analysis/', include((requirement_urlpatterns, 'requirement_schema'))),
    path('api/testcases/', include((testcase_urlpatterns, 'testcase_schema'))),
    path('api/testsuites/', include((testsuite_urlpatterns, 'testsuite_schema'))),
]
XLSX_MEDIA_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


@pytest.fixture(scope='module')
def contract_schema():
    return SchemaGenerator(patterns=SCHEMA_PATTERNS).get_schema(request=None, public=True)


def resolve(schema, document):
    while '$ref' in schema:
        reference = schema['$ref'].removeprefix('#/').split('/')
        target = document
        for part in reference:
            target = target[part.replace('~1', '/').replace('~0', '~')]
        schema = target
    if 'allOf' in schema:
        merged = {key: value for key, value in schema.items() if key != 'allOf'}
        for part in schema['allOf']:
            merged.update(resolve(part, document))
        return merged
    return schema


def operation(schema, endpoint, method):
    return schema['paths'][endpoint][method.lower()]


def response_schema(schema, endpoint_operation, status_code, media_type='application/json'):
    content = endpoint_operation['responses'][str(status_code)]['content']
    return resolve(content[media_type]['schema'], schema)


def request_schema(schema, endpoint_operation, media_type='application/json'):
    content = endpoint_operation['requestBody']['content']
    return resolve(content[media_type]['schema'], schema)


def test_requirement_analysis_requests_responses_and_config_are_typed(contract_schema):
    schema = contract_schema
    analyze = operation(schema, '/api/requirement-analysis/analyze-text/', 'post')
    analyze_request = request_schema(schema, analyze)
    assert set(analyze_request['required']) == {'title', 'description'}
    assert {'title', 'description', 'project'} <= set(analyze_request['properties'])
    assert set(analyze['responses']) == {'202', '400', '401', '403', '500'}
    queued = response_schema(schema, analyze, 202)
    assert set(queued['required']) == {'message', 'document_id', 'task_id'}
    assert {'error'} <= set(response_schema(schema, analyze, 403)['properties'])

    upload = operation(schema, '/api/requirement-analysis/upload-and-analyze/', 'post')
    assert set(upload['requestBody']['content']) == {'multipart/form-data'}
    upload_request = request_schema(schema, upload, 'multipart/form-data')
    assert {'title', 'file'} <= set(upload_request['required'])
    assert 'project' not in upload_request['required']
    assert 'id' not in upload_request['properties']
    assert upload_request['properties']['project']['nullable'] is True
    assert upload_request['properties']['file']['format'] == 'binary'
    validation = response_schema(schema, upload, 400)
    upload_errors = resolve(validation['properties']['error'], schema)
    assert set(upload_errors['properties']) == {'title', 'file', 'project'}

    config = operation(schema, '/api/requirement-analysis/config/check/', 'get')
    config_response = response_schema(schema, config, 200)
    assert set(config_response['required']) == {
        'overall_status', 'message', 'writer_model', 'writer_prompt',
        'reviewer_model', 'reviewer_prompt', 'generation_config',
    }
    overall_status = resolve(config_response['properties']['overall_status'], schema)
    assert set(overall_status['enum']) == {
        'enabled', 'disabled', 'not_configured',
    }
    writer_model = resolve(config_response['properties']['writer_model'], schema)
    reviewer_model = resolve(config_response['properties']['reviewer_model'], schema)
    assert 'provider' in writer_model['required']
    assert 'provider' not in reviewer_model['properties']
    assert set(config['responses']) == {'200', '500'}
    validate_schema(schema)


def test_sync_and_download_contracts_include_access_errors_and_binary_media(contract_schema):
    schema = contract_schema
    sync_path = '/api/testcases/sync-profiles/{id}/sync/'
    sync = operation(schema, sync_path, 'post')
    assert sync['parameters'] == [{
        'in': 'path', 'name': 'id', 'schema': {'type': 'integer'}, 'required': True,
    }]
    sync_response = response_schema(schema, sync, 200)
    assert set(sync_response['required']) == {
        'profile_id', 'provider', 'commit', 'files', 'created', 'updated',
        'skipped', 'conflicts', 'warnings',
    }
    assert set(sync['responses']) == {'200', '400', '401', '404'}
    assert set(response_schema(schema, sync, 404)['properties']) == {'error'}
    assert 'requestBody' not in sync

    template = operation(schema, '/api/testcases/import/template/', 'get')
    template_media = template['responses']['200']['content']
    assert set(template_media) == {XLSX_MEDIA_TYPE}
    assert template_media[XLSX_MEDIA_TYPE]['schema'] == {'type': 'string', 'format': 'binary'}
    assert set(template['responses']) == {'200', '401'}

    report = operation(
        schema,
        '/api/testcases/import-records/{id}/failure-report/',
        'get',
    )
    assert set(report['responses']) == {'200', '401', '404'}
    assert report['responses']['200']['content'][XLSX_MEDIA_TYPE]['schema'] == {
        'type': 'string', 'format': 'binary',
    }
    assert set(response_schema(schema, report, 404)['properties']) == {'error'}
    assert report['parameters'][0]['name'] == 'id'
    assert report['parameters'][0]['required'] is True


def test_suite_single_batch_reorder_and_delete_contracts_stay_distinct(contract_schema):
    schema = contract_schema
    list_path = '/api/testsuites/{id}/testcases/'
    members = operation(schema, list_path, 'get')
    member_list = response_schema(schema, members, 200)
    membership = resolve(member_list['items'], schema)
    assert set(membership['required']) == {
        'id', 'testcase', 'testcase_id', 'test_case_id', 'order',
    }
    case_summary = resolve(membership['properties']['testcase'], schema)
    assert {'id', 'title', 'description', 'project', 'priority', 'status', 'test_type'} <= set(case_summary['properties'])
    assert set(members['responses']) == {'200', '401', '404'}
    assert set(schema['paths']['/api/testsuites/{id}/test_cases/']) >= {'get', 'post'}

    single_paths = [list_path, '/api/testsuites/{id}/add_test_case/']
    for endpoint in single_paths:
        single = operation(schema, endpoint, 'post')
        single_request = request_schema(schema, single)
        variants = [resolve(variant, schema) for variant in single_request['oneOf']]
        assert {tuple(variant['required']) for variant in variants} == {
            ('test_case_id',), ('testcase_id',),
        }
        assert set(single['responses']) == {'201', '400', '401', '404'}
        assert set(response_schema(schema, single, 201)['properties']) == set(membership['properties'])

    batch = operation(schema, '/api/testsuites/{id}/add_test_cases/', 'post')
    batch_request = request_schema(schema, batch)
    batch_variants = [resolve(variant, schema) for variant in batch_request['oneOf']]
    assert {tuple(variant['required']) for variant in batch_variants} == {
        ('test_case_ids',), ('testcase_ids',), ('testcases',),
    }
    ids_schema = batch_variants[0]['properties']['test_case_ids']
    assert any(choice.get('type') == 'array' for choice in ids_schema['oneOf'])
    batch_response = response_schema(schema, batch, 201)
    assert set(batch_response['required']) == {'success', 'added', 'testcases'}
    assert batch_response['properties']['added']['type'] == 'integer'

    reorder = operation(schema, '/api/testsuites/{id}/update_test_case_order/', 'post')
    reorder_request = request_schema(schema, reorder)
    reorder_variants = [resolve(variant, schema) for variant in reorder_request['oneOf']]
    assert {tuple(variant['required']) for variant in reorder_variants} == {
        ('test_case_orders',), ('testcase_orders',),
    }
    orders = reorder_variants[0]['properties']['test_case_orders']
    item_options = orders['items']['oneOf']
    assert {tuple(option['required']) for option in item_options} == {
        ('test_case_id', 'order'), ('testcase_id', 'order'),
    }
    reorder_response = response_schema(schema, reorder, 200)
    assert set(reorder_response['required']) == {'success', 'updated', 'testcases'}
    assert set(operation(schema, '/api/testsuites/{id}/reorder_testcases/', 'post')['responses']) == {
        '200', '400', '401', '404',
    }

    remove_path = '/api/testsuites/{id}/remove_test_case/'
    for method in ('post', 'delete'):
        remove = operation(schema, remove_path, method)
        assert 'requestBody' in remove
        assert set(remove['responses']) == {'200', '400', '401', '404'}
        assert set(response_schema(schema, remove, 200)['required']) == {
            'success', 'deleted', 'test_case_id',
        }

    detail = operation(
        schema,
        '/api/testsuites/{id}/testcases/{testcase_id}/',
        'delete',
    )
    assert {(param['name'], param['in'], param['required']) for param in detail['parameters']} == {
        ('id', 'path', True), ('testcase_id', 'path', True),
    }
    assert set(response_schema(schema, detail, 200)['required']) == {
        'success', 'deleted', 'test_case_id',
    }
    assert set(response_schema(schema, detail, 404)['properties']) == {'detail'}
