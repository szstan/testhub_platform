from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import AppCustomComponent, AppProject, AppTestCase, AppTestExecution
from apps.app_automation.runners import ui_flow_runner as runner_module
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner
from apps.app_automation.serializers import AppCustomComponentSerializer
from apps.app_automation.scene_execution import ScenePreparationError, prepare_scene_execution
from apps.app_automation.views.test_case_views import AppTestCaseViewSet
from apps.users.models import User


@pytest.fixture(autouse=True)
def isolate_privacy_latch_for_taint_unit_tests(monkeypatch):
    from airtest.core import api as airtest_api, cv as airtest_cv
    from apps.app_automation import screenshot_privacy

    monkeypatch.setattr(screenshot_privacy, '_persistence_disabled', False)
    monkeypatch.setattr(airtest_api.ST, 'SAVE_IMAGE', airtest_api.ST.SAVE_IMAGE)
    for module, name in ((airtest_api, 'snapshot'), (airtest_api, 'try_log_screen'),
                         (airtest_cv, 'try_log_screen')):
        monkeypatch.setattr(module, name, getattr(module, name))


class JsonResponse:
    status_code = 200
    headers = {'Content-Type': 'application/json'}

    def json(self):
        return {'data': {'access_token': '${unknown_function(SYNTHETIC_SECRET_42)}'}}


def _runner():
    runner = object.__new__(UiFlowRunner)
    runner._init_context([], {})
    runner._require_device = Mock(return_value=SimpleNamespace(snapshot=lambda **_kwargs: b'fake-screen'))
    runner._load_custom_component_defs = lambda: {}
    return runner


def _run_sensitive_response_flow(monkeypatch, *, alias_route, action):
    runner = _runner()
    protected = Mock()
    ordinary = Mock()
    runner._enter_sensitive_text = protected
    monkeypatch.setattr(runner_module, 'airtest_text', ordinary)
    monkeypatch.setattr(runner, '_resolve_selector', lambda _step: None)
    request_step = {
        'type': 'api_request',
        'url': 'https://api.example.test/token',
        'response_type': 'json',
        'save_as': 'response',
    }
    if alias_route == 'extracts':
        request_step['extracts'] = [{
            'path': 'body.data.access_token', 'name': 'opaque', 'scope': 'local',
        }]
    with (
        patch.object(runner_module, 'validate_outbound_url', side_effect=lambda url, **_kw: url),
        patch.object(runner_module, 'guarded_request', return_value=JsonResponse()),
        patch('apps.core.variable_resolver.resolve_variables') as resolve_variables,
    ):
        runner._execute_step(request_step)
        response_token = '${unknown_function(SYNTHETIC_SECRET_42)}'
        safe_output = runner._sanitize_sensitive_value(runner.context['outputs']['last'])
        assert response_token not in str(safe_output)
        error = ValueError(response_token)
        runner._sanitize_exception_for_reporting(error)
        assert response_token not in str(error)
        if alias_route == 'extract_output':
            runner._execute_step({
                'type': 'extract_output', 'source': 'local.response',
                'path': 'body.data.access_token', 'name': 'opaque', 'scope': 'local',
            })
        runner._execute_step({
            'type': 'set_variable', 'name': 'opaque_alias',
            'value': '{{local.opaque}}', 'scope': 'local',
        })
        value_field = 'text' if action == 'text' else 'value'
        runner._execute_step({'type': action, value_field: '{{local.opaque_alias}}'})

    protected.assert_called_once_with('${unknown_function(SYNTHETIC_SECRET_42)}')
    ordinary.assert_not_called()
    resolve_variables.assert_not_called()


@pytest.mark.parametrize('alias_route', ['extracts', 'extract_output'])
@pytest.mark.parametrize('action', ['input', 'text'])
def test_api_response_secret_stays_tainted_through_aliases(monkeypatch, alias_route, action):
    _run_sensitive_response_flow(monkeypatch, alias_route=alias_route, action=action)


def _post_case(user, project, flow):
    request = APIRequestFactory().post('/app-test-cases/', {
        'name': 'synthetic legacy sensitive case',
        'project': project.pk,
        'ui_flow': flow,
    }, format='json')
    force_authenticate(request, user=user)
    return AppTestCaseViewSet.as_view({'post': 'create'})(request)


@pytest.mark.django_db
def test_legacy_global_component_sensitive_schema_rejects_literal_and_protects_reference(monkeypatch):
    user = User.objects.create_user(username='legacy-sensitive-runtime-user')
    project = AppProject.objects.create(name='legacy sensitive runtime', owner=user)
    component = AppCustomComponent.objects.create(
        name='synthetic legacy login',
        type='synthetic_legacy_login',
        schema={'properties': {'credential': {'type': 'string', 'sensitive': True}}},
        steps=[{'type': 'input', 'config': {'value': '{{local.credential}}'}}],
    )
    private_value = 'SYNTHETIC_PRIVATE_CREDENTIAL_91'
    literal_flow = [{
        'kind': 'custom', 'type': component.type,
        'config': {'credential': private_value},
    }]

    rejected = _post_case(user, project, literal_flow)
    rejected_legacy_action = _post_case(user, project, [{
        'type': component.type, 'config': {'credential': private_value},
    }])

    assert rejected.status_code == 400
    assert rejected_legacy_action.status_code == 400
    assert private_value not in str(rejected.data)
    assert private_value not in str(rejected_legacy_action.data)

    flow = [{
        'type': component.type,
        'config': {'credential': '{{local.runtime_credential}}'},
    }]
    case = AppTestCase.objects.create(
        name='synthetic legacy reference case', project=project, created_by=user,
        ui_flow=flow,
        variables=[{'name': 'runtime_credential', 'scope': 'local', 'value': private_value}],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=user)
    prepared, variables = prepare_scene_execution(case, execution)
    runner = object.__new__(UiFlowRunner)
    runner._init_context(variables, {})
    runner._require_device = Mock(return_value=SimpleNamespace(snapshot=lambda **_kwargs: b'fake-screen'))
    protected = Mock()
    ordinary = Mock()
    runner._enter_sensitive_text = protected
    runner._load_custom_component_defs = lambda: {
        component.type: {
            'schema': component.schema,
            'default_config': component.default_config,
            'steps': component.steps,
        },
    }
    monkeypatch.setattr(runner_module, 'airtest_text', ordinary)
    monkeypatch.setattr(runner, '_resolve_selector', lambda _step: None)

    runner._execute_step(prepared[0])

    protected.assert_called_once_with(private_value)
    ordinary.assert_not_called()


@pytest.mark.django_db
def test_inline_legacy_component_sensitive_schema_rejects_literal():
    user = User.objects.create_user(username='legacy-inline-sensitive-user')
    project = AppProject.objects.create(name='legacy inline sensitive', owner=user)
    private_value = 'SYNTHETIC_INLINE_PRIVATE_73'
    flow = [{
        'kind': 'custom', 'type': 'inline_sensitive_fixture',
        'schema': {'properties': {'credential': {'type': 'string', 'writeOnly': True}}},
        'config': {'credential': private_value},
        'steps': [{'type': 'input', 'config': {'value': '{{local.credential}}'}}],
    }]

    response = _post_case(user, project, flow)

    assert response.status_code == 400
    assert private_value not in str(response.data)


@pytest.mark.django_db
@pytest.mark.parametrize('location', ['default_config', 'input_step'])
def test_global_component_serializer_rejects_sensitive_literals(location):
    private_value = f'SYNTHETIC_GLOBAL_DEFINITION_SECRET_{location}'
    payload = {
        'name': f'global sensitive definition {location}',
        'type': f'global_sensitive_definition_{location}',
        'schema': {'properties': {'credential': {'type': 'string', 'sensitive': True}}},
        'default_config': {},
        'steps': [{'type': 'input', 'config': {'value': '{{local.credential}}'}}],
        'enabled': True,
        'sort_order': 0,
    }
    if location == 'default_config':
        payload['default_config']['credential'] = private_value
    else:
        payload['steps'] = [{
            'type': 'input', 'sensitive': True, 'value': private_value,
        }]

    serializer = AppCustomComponentSerializer(data=payload)

    assert not serializer.is_valid()
    assert private_value not in str(serializer.errors)


@pytest.mark.django_db
def test_legacy_global_component_is_revalidated_before_execution():
    user = User.objects.create_user(username='legacy-sensitive-prepare-user')
    project = AppProject.objects.create(name='legacy sensitive prepare', owner=user)
    component = AppCustomComponent.objects.create(
        name='synthetic legacy login prepare',
        type='synthetic_legacy_login_prepare',
        schema={'properties': {'credential': {'type': 'string', 'secret': True}}},
        steps=[{'type': 'input', 'config': {'value': '{{local.credential}}'}}],
    )
    private_value = 'SYNTHETIC_STALE_PRIVATE_58'
    case = AppTestCase.objects.create(
        name='synthetic old saved flow', project=project, created_by=user,
        ui_flow=[{'kind': 'custom', 'type': component.type,
                  'config': {'credential': private_value}}],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=user)

    with pytest.raises(ScenePreparationError) as error:
        prepare_scene_execution(case, execution)

    assert private_value not in str(error.value)


@pytest.mark.django_db
@pytest.mark.parametrize('custom_kind', [False, True])
@pytest.mark.parametrize('inline_schema', [{}, {'properties': {'opaque_field': {'type': 'string', 'sensitive': False}}}])
def test_invocation_cannot_weaken_stored_global_sensitive_contract(monkeypatch, custom_kind, inline_schema):
    user = User.objects.create_user(username='stored-opaque-contract-user')
    project = AppProject.objects.create(name='stored opaque contract', owner=user)
    component = AppCustomComponent.objects.create(
        name='opaque contract component', type='opaque_contract_fixture',
        schema={'properties': {'opaque_field': {'type': 'string', 'sensitive': True}}},
        steps=[{'type': 'input', 'config': {'value': '{{local.opaque_field}}'}}],
    )
    private_value = 'SYNTHETIC_OPAQUE_PRIVATE_82'
    step = {'type': component.type, 'schema': inline_schema, 'config': {'opaque_field': private_value}}
    if custom_kind:
        step['kind'] = 'custom'
    rejected = _post_case(user, project, [step])
    assert rejected.status_code == 400
    assert private_value not in str(rejected.data)

    case = AppTestCase.objects.create(name='old opaque case', project=project, created_by=user, ui_flow=[step])
    execution = AppTestExecution.objects.create(test_case=case, user=user)
    with pytest.raises(ScenePreparationError) as error:
        prepare_scene_execution(case, execution)
    assert private_value not in str(error.value)

    runner = _runner()
    runner._load_custom_component_defs = lambda: {component.type: {
        'schema': component.schema, 'default_config': component.default_config, 'steps': component.steps,
    }}
    protected, ordinary = Mock(), Mock()
    runner._enter_sensitive_text = protected
    monkeypatch.setattr(runner_module, 'airtest_text', ordinary)
    with pytest.raises(ValueError) as error:
        runner._execute_step(step)
    assert private_value not in str(error.value)
    protected.assert_not_called()
    ordinary.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize('contract_source', ['schema', 'stored_steps'])
def test_weaker_inline_contract_cannot_remove_reference_taint(monkeypatch, contract_source):
    user = User.objects.create_user(username='opaque-reference-contract-user')
    project = AppProject.objects.create(name='opaque reference contract', owner=user)
    stored_step = {'type': 'input', 'config': {'value': '{{local.opaque_field}}'}}
    schema = {'properties': {'opaque_field': {'type': 'string', 'writeOnly': True}}}
    if contract_source == 'stored_steps':
        schema = {}
        stored_step['sensitive'] = True
    component = AppCustomComponent.objects.create(
        name='opaque reference component', type='opaque_reference_fixture', schema=schema, steps=[stored_step],
    )
    private_value = '${unknown_function(SYNTHETIC_OPAQUE_REFERENCE_49)}'
    step = {
        'kind': 'custom', 'type': component.type, 'schema': {}, 'default_config': {},
        'config': {'opaque_field': '{{local.ordinary_name}}'},
        'steps': [{'type': 'input', 'config': {'value': '{{local.opaque_field}}'}}],
    }
    accepted = _post_case(user, project, [step])
    assert accepted.status_code == 201, accepted.data
    case = AppTestCase.objects.get(pk=accepted.data['id'])
    case.variables = [{'name': 'ordinary_name', 'value': private_value}]
    case.save(update_fields=['variables'])
    execution = AppTestExecution.objects.create(test_case=case, user=user)
    prepared, variables = prepare_scene_execution(case, execution)
    runner = _runner()
    runner._init_context(variables, {})
    runner._load_custom_component_defs = lambda: {component.type: {
        'schema': component.schema, 'default_config': component.default_config, 'steps': component.steps,
    }}
    runner._resolve_selector = lambda _step: None
    protected, ordinary = Mock(), Mock()
    runner._enter_sensitive_text = protected
    monkeypatch.setattr(runner_module, 'airtest_text', ordinary)
    with patch('apps.core.variable_resolver.resolve_variables') as dynamic:
        runner._execute_step(prepared[0])
    protected.assert_called_once_with(private_value)
    ordinary.assert_not_called()
    dynamic.assert_not_called()
