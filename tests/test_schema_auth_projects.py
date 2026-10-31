import pytest
from django.contrib.auth import get_user_model
from django.urls import include, path
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker, ValidationError
from rest_framework.test import APIClient
from unittest.mock import Mock, patch

from apps.projects.models import Project, ProjectMember
from apps.versions.models import Version


urlpatterns = [
    path('api/auth/', include('apps.users.urls')),
    path('api/users/', include('apps.users.urls')),
    path('api/projects/', include('apps.projects.urls')),
    path('api/versions/', include('apps.versions.urls')),
]

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.urls('tests.test_schema_auth_projects'),
]


@pytest.fixture(scope='module')
def openapi_schema():
    return SchemaGenerator(patterns=urlpatterns).get_schema(request=None, public=True)


def _operation(schema, path, method):
    return schema['paths'][path][method.lower()]


def _resolve(schema, value):
    while '$ref' in value:
        name = value['$ref'].rsplit('/', 1)[-1]
        value = schema['components']['schemas'][name]
    return value


def _request_schema(schema, operation):
    body = operation['requestBody']['content']['application/json']['schema']
    return _resolve(schema, body)


def _response_schema(schema, operation, status_code):
    response = operation['responses'][str(status_code)]
    return _resolve(schema, response['content']['application/json']['schema'])


def _properties(schema, value):
    return _resolve(schema, value).get('properties', {})


def _property_names(schema, value):
    return frozenset(_properties(schema, value))


def test_schema_covers_all_named_auth_project_and_version_operations(openapi_schema):
    required = {
        ('/api/auth/captcha/', 'get'): {'200', '503'},
        ('/api/auth/exchange-token/', 'post'): {'200', '400', '401'},
        ('/api/auth/login/', 'post'): {'200', '400', '429'},
        ('/api/auth/logout/', 'post'): {'200'},
        ('/api/auth/me/', 'get'): {'200', '401'},
        ('/api/auth/profile/', 'get'): {'200', '401'},
        ('/api/auth/send-register-code/', 'post'): {'200', '400'},
        ('/api/auth/sms-login/', 'post'): {'200', '400'},
        ('/api/auth/websocket-ticket/', 'post'): {'200', '400', '401', '404'},
        ('/api/users/captcha/', 'get'): {'200', '503'},
        ('/api/users/exchange-token/', 'post'): {'200', '400', '401'},
        ('/api/users/login/', 'post'): {'200', '400', '429'},
        ('/api/users/logout/', 'post'): {'200'},
        ('/api/users/me/', 'get'): {'200', '401'},
        ('/api/users/profile/', 'get'): {'200', '401'},
        ('/api/users/send-register-code/', 'post'): {'200', '400'},
        ('/api/users/sms-login/', 'post'): {'200', '400'},
        ('/api/users/websocket-ticket/', 'post'): {'200', '400', '401', '404'},
        ('/api/projects/all/', 'get'): {'200', '401'},
        ('/api/projects/{project_id}/members/', 'get'): {'200', '401', '403', '404'},
        ('/api/projects/{project_id}/members/add/', 'post'): {'201', '400', '401', '403', '404'},
        ('/api/projects/{project_id}/members/{member_id}/', 'delete'): {'200', '401', '403', '404'},
        ('/api/projects/list/', 'get'): {'200', '401'},
        ('/api/versions/projects/{project_id}/versions/', 'get'): {'200', '401', '403'},
    }
    for (path, method), statuses in required.items():
        operation = _operation(openapi_schema, path, method)
        assert statuses <= set(operation['responses']), f'{method.upper()} {path}'


def test_schema_request_fields_requiredness_write_only_and_response_privacy(openapi_schema):
    login = _operation(openapi_schema, '/api/auth/login/', 'post')
    login_request = _request_schema(openapi_schema, login)
    assert set(login_request['required']) == {'username', 'password'}
    assert login_request['properties']['password']['writeOnly'] is True
    login_response = _response_schema(openapi_schema, login, 200)
    login_properties = _properties(openapi_schema, login_response)
    assert {'user', 'access', 'message'} <= set(login_properties)
    assert 'refresh' not in login_properties
    session_properties = _properties(openapi_schema, login_properties['user'])
    assert {'id', 'username', 'email', 'avatar', 'is_active', 'is_admin'} <= set(session_properties)
    assert not {'password', 'refresh', 'is_staff'} & set(session_properties)

    logout = _operation(openapi_schema, '/api/auth/logout/', 'post')
    logout_request = _request_schema(openapi_schema, logout)
    assert logout_request['properties']['refresh']['writeOnly'] is True
    assert 'refresh' not in logout_request.get('required', [])

    send_code = _operation(openapi_schema, '/api/auth/send-register-code/', 'post')
    send_request = _request_schema(openapi_schema, send_code)
    assert set(send_request['required']) == {'phone', 'captcha_token', 'captcha_code'}
    assert send_request['properties']['captcha_token']['writeOnly'] is True
    assert send_request['properties']['captcha_code']['writeOnly'] is True
    assert 'mode' not in send_request.get('required', [])

    sms_login = _operation(openapi_schema, '/api/auth/sms-login/', 'post')
    sms_request = _request_schema(openapi_schema, sms_login)
    assert set(sms_request['required']) == {'phone', 'verify_code', 'verify_code_token'}
    assert sms_request['properties']['verify_code']['writeOnly'] is True
    assert sms_request['properties']['verify_code_token']['writeOnly'] is True
    assert sms_request['properties']['phone']['pattern'] == r'^1[3-9]\d{9}$'

    exchange = _operation(openapi_schema, '/api/auth/exchange-token/', 'post')
    exchange_request = _request_schema(openapi_schema, exchange)
    assert exchange_request['required'] == ['action']
    assert exchange_request['properties']['code']['writeOnly'] is True
    assert 'code' not in exchange_request['required']
    exchange_success = _response_schema(openapi_schema, exchange, 200)
    assert len(exchange_success['oneOf']) == 2
    exchange_variants = [_properties(openapi_schema, variant) for variant in exchange_success['oneOf']]
    assert any(set(properties) == {'code'} for properties in exchange_variants)
    assert any({'access', 'user'} <= set(properties) for properties in exchange_variants)

    ticket = _operation(openapi_schema, '/api/auth/websocket-ticket/', 'post')
    ticket_request = _request_schema(openapi_schema, ticket)
    assert set(ticket_request['required']) == {'channel', 'execution_id'}

    add_member = _operation(openapi_schema, '/api/projects/{project_id}/members/add/', 'post')
    member_request = _request_schema(openapi_schema, add_member)
    assert member_request['required'] == ['user_id']
    assert member_request['properties']['user_id']['writeOnly'] is True
    assert {'role', 'joined_at'} <= set(member_request['properties'])


def test_schema_preserves_bare_arrays_envelopes_and_handler_specific_shapes(openapi_schema):
    captcha = _operation(openapi_schema, '/api/auth/captcha/', 'get')
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, captcha, 200))) == {'token', 'image'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, captcha, 503))) == {'success', 'error'}

    login = _operation(openapi_schema, '/api/auth/login/', 'post')
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, login, 400))) == {
        'username', 'password', 'non_field_errors',
    }
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, login, 429))) == {'error'}

    logout = _operation(openapi_schema, '/api/auth/logout/', 'post')
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, logout, 200))) == {'message'}

    session_fields = {
        'id', 'username', 'email', 'first_name', 'last_name', 'avatar', 'phone', 'department',
        'position', 'is_active', 'date_joined', 'created_at', 'updated_at', 'is_admin',
    }
    current_user = _operation(openapi_schema, '/api/auth/me/', 'get')
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, current_user, 200))) == session_fields
    profile = _operation(openapi_schema, '/api/auth/profile/', 'get')
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, profile, 200))) == session_fields
    profile_unauthorized = _response_schema(openapi_schema, profile, 401)
    assert len(profile_unauthorized['oneOf']) == 2
    assert {_property_names(openapi_schema, variant) for variant in profile_unauthorized['oneOf']} == {
        frozenset({'error'}), frozenset({'detail'}),
    }

    send_code = _operation(openapi_schema, '/api/auth/send-register-code/', 'post')
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, send_code, 200))) == {
        'success', 'verify_code_token', 'message',
    }
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, send_code, 400))) == {'success', 'error'}

    sms_login = _operation(openapi_schema, '/api/auth/sms-login/', 'post')
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, sms_login, 200))) == {
        'user', 'access', 'message',
    }
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, sms_login, 400))) == {'error'}

    ticket = _operation(openapi_schema, '/api/auth/websocket-ticket/', 'post')
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, ticket, 200))) == {
        'ticket', 'expires_in_seconds', 'path',
    }
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, ticket, 400))) == {'error'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, ticket, 401))) == {'detail'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, ticket, 404))) == {'error'}

    project_array = _response_schema(
        openapi_schema, _operation(openapi_schema, '/api/projects/all/', 'get'), 200,
    )
    assert project_array['type'] == 'array'
    assert set(_properties(openapi_schema, project_array['items'])) == {
        'id', 'name', 'description', 'status',
    }
    assert set(_properties(
        openapi_schema,
        _response_schema(openapi_schema, _operation(openapi_schema, '/api/projects/all/', 'get'), 401),
    )) == {'detail'}

    member_list = _operation(openapi_schema, '/api/projects/{project_id}/members/', 'get')
    member_array = _response_schema(openapi_schema, member_list, 200)
    assert member_array['type'] == 'array'
    assert set(_properties(openapi_schema, member_array['items'])) == {
        'id', 'username', 'email', 'first_name', 'last_name', 'role',
    }
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, member_list, 401))) == {'detail'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, member_list, 403))) == {'error'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, member_list, 404))) == {'error'}

    add_member = _operation(openapi_schema, '/api/projects/{project_id}/members/add/', 'post')
    add_response = _response_schema(openapi_schema, add_member, 201)
    assert set(_properties(openapi_schema, add_response)) == {'id', 'user', 'role', 'joined_at'}
    assert set(_properties(openapi_schema, _properties(openapi_schema, add_response)['user'])) == {
        'id', 'username', 'email', 'first_name', 'last_name', 'avatar', 'phone', 'department', 'position',
        'is_active', 'date_joined', 'created_at', 'updated_at',
    }
    assert 'user_id' not in _properties(openapi_schema, add_response)
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, add_member, 400))) == {
        'user_id', 'role', 'joined_at', 'non_field_errors',
    }
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, add_member, 401))) == {'detail'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, add_member, 403))) == {'error'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, add_member, 404))) == {'error'}

    remove_member = _operation(openapi_schema, '/api/projects/{project_id}/members/{member_id}/', 'delete')
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, remove_member, 200))) == {'message'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, remove_member, 401))) == {'detail'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, remove_member, 403))) == {'error'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, remove_member, 404))) == {'error'}

    dropdown = _response_schema(
        openapi_schema, _operation(openapi_schema, '/api/projects/list/', 'get'), 200,
    )
    assert set(_properties(openapi_schema, dropdown)) == {'results'}
    assert _properties(openapi_schema, dropdown)['results']['type'] == 'array'
    assert set(_properties(openapi_schema, _properties(openapi_schema, dropdown)['results']['items'])) == {
        'id', 'name', 'status',
    }
    assert set(_properties(
        openapi_schema,
        _response_schema(openapi_schema, _operation(openapi_schema, '/api/projects/list/', 'get'), 401),
    )) == {'detail'}

    versions_operation = _operation(openapi_schema, '/api/versions/projects/{project_id}/versions/', 'get')
    versions = _response_schema(openapi_schema, versions_operation, 200)
    assert versions['type'] == 'array'
    assert set(_properties(openapi_schema, versions['items'])) == {
        'id', 'name', 'description', 'is_baseline', 'projects', 'created_by', 'created_at', 'testcases_count',
    }
    version_properties = _properties(openapi_schema, versions['items'])
    assert set(_properties(openapi_schema, version_properties['projects']['items'])) == {'id', 'name'}
    assert set(_properties(openapi_schema, version_properties['created_by'])) == {
        'id', 'username', 'email', 'first_name', 'last_name', 'avatar', 'phone', 'department', 'position',
        'is_active', 'date_joined', 'created_at', 'updated_at',
    }
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, versions_operation, 401))) == {'detail'}
    assert set(_properties(openapi_schema, _response_schema(openapi_schema, versions_operation, 403))) == {'error'}


@pytest.mark.parametrize('email', ['', 'session-user@example.test'])
@pytest.mark.parametrize('path', ['/api/auth/me/', '/api/auth/profile/'])
def test_session_email_schema_accepts_actual_empty_and_configured_profiles(openapi_schema, email, path):
    user = get_user_model().objects.create_user(username='session-email-fixture', email=email)
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get(path)
    assert response.status_code == 200
    assert response.data['email'] == email
    operation = _operation(openapi_schema, path, 'get')
    email_schema = _properties(openapi_schema, _response_schema(openapi_schema, operation, 200))['email']
    validator = Draft4Validator(email_schema, format_checker=FormatChecker())
    validator.validate(response.data['email'])
    with pytest.raises(ValidationError):
        validator.validate('invalid-email')
    with pytest.raises(ValidationError):
        validator.validate(None)


@pytest.fixture
def project_records(db):
    user_model = get_user_model()
    owner = user_model.objects.create_user(
        username='schema-owner', email='schema-owner@example.test', password='test-password-only',
        phone='13800138000',
    )
    member = user_model.objects.create_user(
        username='schema-member', email='schema-member@example.test', password='test-password-only',
    )
    new_member = user_model.objects.create_user(
        username='schema-new-member', email='schema-new-member@example.test', password='test-password-only',
    )
    project = Project.objects.create(name='Schema contract project', owner=owner)
    ProjectMember.objects.create(project=project, user=member, role='tester')
    version = Version.objects.create(name='Schema contract version', created_by=owner)
    version.projects.add(project)
    client = APIClient()
    client.force_authenticate(user=owner)
    return {
        'client': client,
        'owner': owner,
        'member': member,
        'new_member': new_member,
        'project': project,
        'version': version,
    }


def test_fixture_project_and_version_responses_match_documented_shapes(project_records):
    client = project_records['client']
    owner = project_records['owner']
    project = project_records['project']
    version = project_records['version']

    current_user = client.get('/api/auth/me/')
    assert current_user.status_code == 200
    assert {'id', 'username', 'is_admin'} <= set(current_user.data)
    assert not {'password', 'is_staff', 'refresh'} & set(current_user.data)

    all_projects = client.get('/api/projects/all/')
    assert all_projects.status_code == 200
    assert isinstance(all_projects.data, list)
    assert set(all_projects.data[0]) == {'id', 'name', 'description', 'status'}

    members = client.get(f'/api/projects/{project.pk}/members/')
    assert members.status_code == 200
    assert isinstance(members.data, list)
    assert set(members.data[0]) == {'id', 'username', 'email', 'first_name', 'last_name', 'role'}
    assert {item['role'] for item in members.data} == {'owner', 'tester'}

    dropdown = client.get('/api/projects/list/')
    assert dropdown.status_code == 200
    assert set(dropdown.data) == {'results'}
    assert set(dropdown.data['results'][0]) == {'id', 'name', 'status'}

    versions = client.get(f'/api/versions/projects/{project.pk}/versions/')
    assert versions.status_code == 200
    assert len(versions.data) == 1
    assert versions.data[0]['id'] == version.pk
    assert set(versions.data[0]) == {
        'id', 'name', 'description', 'is_baseline', 'projects', 'created_by', 'created_at', 'testcases_count',
    }
    assert versions.data[0]['created_by']['id'] == owner.pk
    assert versions.data[0]['projects'] == [{'id': project.pk, 'name': project.name}]
    assert set(versions.data[0]['created_by']) == {
        'id', 'username', 'email', 'first_name', 'last_name', 'avatar', 'phone', 'department', 'position',
        'is_active', 'date_joined', 'created_at', 'updated_at',
    }

    created = client.post(
        f'/api/projects/{project.pk}/members/add/',
        {'user_id': project_records['new_member'].pk, 'role': 'viewer'},
        format='json',
    )
    assert created.status_code == 201
    assert set(created.data) == {'id', 'user', 'role', 'joined_at'}
    assert created.data['user']['id'] == project_records['new_member'].pk
    assert set(created.data['user']) == {
        'id', 'username', 'email', 'first_name', 'last_name', 'avatar', 'phone', 'department', 'position',
        'is_active', 'date_joined', 'created_at', 'updated_at',
    }

    removed = client.delete(f'/api/projects/{project.pk}/members/{created.data["id"]}/')
    assert removed.status_code == 200
    assert set(removed.data) == {'message'}


def test_fixture_auth_responses_and_mocked_external_code_services(project_records):
    client = project_records['client']
    owner = project_records['owner']

    login = client.post(
        '/api/auth/login/',
        {'username': owner.username, 'password': 'test-password-only'},
        format='json',
    )
    assert login.status_code == 200
    assert set(login.data) == {'user', 'access', 'message'}
    assert 'refresh' not in login.data
    assert 'testhub_refresh' in login.cookies

    with patch('apps.users.views.create_captcha', return_value={
        'token': 'fixture-captcha-token',
        'image': 'data:image/png;base64,ZmFrZQ==',
    }):
        captcha = client.get('/api/auth/captcha/')
    assert captcha.status_code == 200
    assert set(captcha.data) == {'token', 'image'}

    with patch('apps.users.views.send_register_verify_code', return_value={
        'success': True,
        'verify_code_token': 'fixture-sms-token',
        'message': 'sent',
    }):
        sent = client.post(
            '/api/auth/send-register-code/',
            {
                'phone': '13900139000',
                'captcha_token': 'fixture-captcha-token',
                'captcha_code': 'ABCD',
            },
            format='json',
        )
    assert sent.status_code == 200
    assert set(sent.data) == {'success', 'verify_code_token', 'message'}

    with patch('apps.users.views.validate_verify_code', return_value=(True, '')):
        sms_login = client.post(
            '/api/auth/sms-login/',
            {
                'phone': owner.phone or '13800138000',
                'verify_code': '000000',
                'verify_code_token': 'fixture-sms-token',
            },
            format='json',
        )
    assert sms_login.status_code == 200
    assert set(sms_login.data) == {'user', 'access', 'message'}
    assert set(sms_login.data['user']) == set(client.get('/api/auth/profile/').data)

    redis_client = Mock()
    with patch('apps.users.views.get_redis', return_value=redis_client):
        exchange = client.post('/api/auth/exchange-token/', {'action': 'create'}, format='json')
    assert exchange.status_code == 200
    assert set(exchange.data) == {'code'}
    redis_client.setex.assert_called_once()

    logout = client.post('/api/auth/logout/', {}, format='json')
    assert logout.status_code == 200
    assert set(logout.data) == {'message'}

    invalid_ticket = client.post(
        '/api/auth/websocket-ticket/',
        {'channel': 'unknown', 'execution_id': '1'},
        format='json',
    )
    assert invalid_ticket.status_code == 400
    assert set(invalid_ticket.data) == {'error'}
