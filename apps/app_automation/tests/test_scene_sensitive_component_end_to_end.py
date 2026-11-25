from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import (
    AppProject,
    AppProjectComponent,
    AppTestCase,
    AppTestExecution,
)
from apps.app_automation.runners.ui_flow_runner import UiFlowRunner
from apps.app_automation.scene_execution import ScenePreparationError, prepare_scene_execution
from apps.app_automation.scene_sensitive_flow import (
    SensitiveFlowError,
    protect_sensitive_inputs,
)
from apps.app_automation.views.test_case_views import AppTestCaseViewSet
from apps.users.models import User


@pytest.fixture(autouse=True)
def isolate_privacy_latch_for_component_unit_tests(monkeypatch):
    from airtest.core import api as airtest_api, cv as airtest_cv
    from apps.app_automation import screenshot_privacy

    monkeypatch.setattr(screenshot_privacy, '_persistence_disabled', False)
    monkeypatch.setattr(airtest_api.ST, 'SAVE_IMAGE', airtest_api.ST.SAVE_IMAGE)
    for module, name in ((airtest_api, 'snapshot'), (airtest_api, 'try_log_screen'),
                         (airtest_cv, 'try_log_screen')):
        monkeypatch.setattr(module, name, getattr(module, name))


@pytest.fixture
def scene_context(db):
    owner = User.objects.create_user(username='sensitive-regression-owner')
    member = User.objects.create_user(username='sensitive-regression-member')
    project = AppProject.objects.create(name='sensitive regression project', owner=owner)
    project.members.add(member)
    return owner, member, project


def _post_case(user, project, flow):
    request = APIRequestFactory().post('/app-test-cases/', {
        'name': 'synthetic component regression case',
        'project': project.pk,
        'ui_flow': flow,
    }, format='json')
    force_authenticate(request, user=user)
    return AppTestCaseViewSet.as_view({'post': 'create'})(request)


def _reference(component, field='params', **values):
    reference = {'kind': 'custom', 'type': component.type, 'version': component.version}
    reference[field] = values
    return reference


def _component(owner, project, *, name, component_type, schema, steps, defaults=None):
    return AppProjectComponent.objects.create(
        project=project,
        name=name,
        type=component_type,
        version='1',
        schema=schema,
        default_config=defaults or {},
        steps=steps,
        created_by=owner,
    )


@pytest.mark.django_db
@pytest.mark.parametrize('schema_flag', ['sensitive', 'writeOnly'])
@pytest.mark.parametrize('expression', [
    '{{local.dynamic_value}}',
    '${global.generic_value}',
    '{{outputs.response.body.access_token}}',
])
def test_component_schema_taint_marks_expanded_text_for_dynamic_expressions(
    scene_context, schema_flag, expression,
):
    owner, member, project = scene_context
    component = _component(
        owner, project, name='sensitive text fixture', component_type='sensitive_text_fixture',
        schema={'properties': {'payload': {'type': 'string', schema_flag: True}}},
        steps=[{'type': 'text', 'config': {'text': '{{params.payload}}'}}],
    )
    flow = [_reference(component, payload=expression)]
    case = AppTestCase.objects.create(
        name='synthetic sensitive text case', project=project, created_by=member, ui_flow=flow,
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)

    expanded, _ = prepare_scene_execution(case, execution)

    assert expanded == [{
        'type': 'text', 'config': {'text': expression, 'sensitive': True},
    }]


@pytest.mark.django_db
@pytest.mark.parametrize('action', ['input', 'text'])
def test_nested_component_forwarding_preserves_schema_taint_for_secure_input_path(
    scene_context, monkeypatch, action,
):
    owner, member, project = scene_context
    value_field = 'value' if action == 'input' else 'text'
    child = _component(
        owner, project, name='sensitive child', component_type='sensitive_child_fixture',
        schema={'properties': {'value': {'type': 'string', 'writeOnly': True}}},
        steps=[{'type': action, 'config': {value_field: '{{params.value}}'}}],
    )
    parent = _component(
        owner, project, name='forwarding parent', component_type='forwarding_parent_fixture',
        schema={'properties': {'generic': {'type': 'string'}}},
        steps=[_reference(child, value='{{params.generic}}')],
    )
    flow = [_reference(parent, generic='${global.generic_value}')]
    case = AppTestCase.objects.create(
        name='synthetic forwarding case', project=project, created_by=member,
        ui_flow=flow,
        variables=[{'name': 'generic_value', 'scope': 'global', 'value': 'synthetic-value'}],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    expanded, variables = prepare_scene_execution(case, execution)
    runner = object.__new__(UiFlowRunner)
    runner._init_context(variables, {})
    runner._require_device = Mock(return_value=SimpleNamespace(snapshot=lambda **_kwargs: b'fake-screen'))
    protected_input = Mock()
    ordinary_input = Mock()
    runner._enter_sensitive_text = protected_input
    monkeypatch.setattr('apps.app_automation.runners.ui_flow_runner.airtest_text', ordinary_input)
    runner._dispatch_step = lambda step, step_action: (
        runner._action_input(step) if step_action == 'input' else runner._action_text(step)
    )
    runner._record_step_output = lambda step, output: None

    runner._execute_step(expanded[0])

    protected_input.assert_called_once_with('synthetic-value')
    ordinary_input.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize('parameter_source', ['params', 'config'])
@pytest.mark.parametrize(
    ('params', 'status_code'),
    [
        ({}, 400),
        ({'amount': 'not-an-integer'}, 400),
        ({'amount': 4}, 201),
        ({'amount': '{{local.runtime_amount}}'}, 201),
    ],
)
def test_case_save_validates_required_and_typed_project_parameters(
    scene_context, parameter_source, params, status_code,
):
    owner, member, project = scene_context
    component = _component(
        owner, project, name='typed required fixture', component_type='typed_required_fixture',
        schema={
            'properties': {'amount': {'type': 'integer'}},
            'required': ['amount'],
        },
        steps=[{'type': 'wait'}],
    )

    response = _post_case(member, project, [_reference(component, parameter_source, **params)])

    assert response.status_code == status_code, response.data
    if status_code == 400:
        assert '项目组件配置无效或无权访问' in str(response.data)
        assert 'not-an-integer' not in str(response.data)


@pytest.mark.django_db
@pytest.mark.parametrize('default_location', ['schema', 'default_config'])
@pytest.mark.parametrize('parameter_source', ['params', 'config'])
def test_schema_defaults_and_legacy_unversioned_components_remain_saveable(
    scene_context, default_location, parameter_source,
):
    owner, member, project = scene_context
    property_spec = {'type': 'integer'}
    defaults = {}
    if default_location == 'schema':
        property_spec['default'] = 7
    else:
        defaults = {'amount': 7}
    component = _component(
        owner, project, name='default fixture', component_type='default_fixture',
        schema={
            'properties': {'amount': property_spec},
            'required': ['amount'],
        },
        steps=[{'type': 'wait'}], defaults=defaults,
    )
    flow = [
        _reference(component, parameter_source),
        {
            'kind': 'custom',
            'type': 'legacy_unversioned_fixture',
            'config': {'legacy_parameter': 'synthetic value'},
            'steps': [{'type': 'wait'}],
        },
    ]

    response = _post_case(member, project, flow)

    assert response.status_code == 201, response.data
    case = AppTestCase.objects.get(pk=response.data['id'])
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    expanded, _ = prepare_scene_execution(case, execution)
    assert expanded[0] == {'type': 'wait'}
    assert expanded[1] == flow[1]


@pytest.mark.django_db
@pytest.mark.parametrize('child_has_default', [False, True])
def test_nested_component_required_validation_uses_same_default_contract(
    scene_context, child_has_default,
):
    owner, member, project = scene_context
    property_spec = {'type': 'integer'}
    if child_has_default:
        property_spec['default'] = 3
    child = _component(
        owner, project, name='nested typed child', component_type='nested_typed_child',
        schema={'properties': {'count': property_spec}, 'required': ['count']},
        steps=[{'type': 'wait'}],
    )
    parent = _component(
        owner, project, name='nested wrapper', component_type='nested_wrapper',
        schema={}, steps=[_reference(child)],
    )

    response = _post_case(member, project, [_reference(parent)])

    assert response.status_code == (201 if child_has_default else 400), response.data


@pytest.mark.django_db
@pytest.mark.parametrize('default_location', ['schema', 'default_config'])
def test_case_save_rejects_invalid_typed_component_defaults(scene_context, default_location):
    owner, member, project = scene_context
    property_spec = {'type': 'integer'}
    defaults = {}
    if default_location == 'schema':
        property_spec['default'] = 'synthetic-not-an-integer'
    else:
        defaults = {'count': 'synthetic-not-an-integer'}
    component = _component(
        owner, project, name='invalid default fixture', component_type='invalid_default_fixture',
        schema={'properties': {'count': property_spec}, 'required': ['count']},
        steps=[{'type': 'wait'}], defaults=defaults,
    )

    response = _post_case(member, project, [_reference(component)])

    assert response.status_code == 400
    assert 'synthetic-not-an-integer' not in str(response.data)


def test_text_sensitivity_uses_runner_field_precedence_and_output_token_names():
    cases = [
        ({'type': 'text', 'value': '{{local.ordinary}}',
          'text': '{{outputs.latest.body.access_token}}'}, True),
        ({'type': 'text', 'value': '{{outputs.latest.body.access_token}}',
          'text': 'synthetic ordinary text'}, False),
        ({'type': 'text', 'value': 'synthetic ordinary text',
          'config': {'text': '${outputs.latest.body.refresh_token}'}}, True),
        ({'type': 'input', 'text': '{{outputs.latest.body.access_token}}',
          'value': 'synthetic ordinary text'}, False),
    ]

    for step, expected_sensitive in cases:
        result = protect_sensitive_inputs([step], [])[0]
        marked = (
            result.get('sensitive') is True
            or result.get('config', {}).get('sensitive') is True
        )
        assert marked is expected_sensitive
        if expected_sensitive and isinstance(result.get('config'), dict):
            assert result['config'].get('sensitive') is True


def _walk_steps(sequence):
    for step in sequence:
        yield step
        for field in ('steps', 'then_steps', 'else_steps', 'try_steps', 'catch_steps', 'finally_steps'):
            nested = step.get(field)
            if isinstance(nested, list):
                yield from _walk_steps(nested)
        config = step.get('config')
        if isinstance(config, dict):
            for field in ('steps', 'then_steps', 'else_steps', 'try_steps', 'catch_steps', 'finally_steps'):
                nested = config.get(field)
                if isinstance(nested, list):
                    yield from _walk_steps(nested)


@pytest.mark.django_db
@pytest.mark.parametrize('component_route', ['versioned', 'inline', 'mixed'])
@pytest.mark.parametrize('action', ['input', 'text'])
def test_sensitive_parent_parameter_reaches_only_matching_nested_inputs(
    scene_context, monkeypatch, component_route, action,
):
    owner, member, project = scene_context
    value_field = 'value' if action == 'input' else 'text'
    child_reference = None
    normal_text = {'type': 'text', 'config': {'text': '正常文案'}}
    child_input = {'type': action, 'config': {value_field: '{{params.payload}}'}}

    if component_route != 'inline':
        child_steps = [child_input, normal_text]
        if component_route == 'mixed':
            child_steps = [{'type': 'if', 'config': {'try_steps': child_steps}}]
        child = _component(
            owner, project, name='plain nested child', component_type='plain_nested_child_fixture',
            schema={'properties': {'payload': {'type': 'string'}}}, steps=child_steps,
        )
        child_reference = _reference(child, payload='{{params.alias}}' if component_route == 'mixed'
                                     else '{{params.pin}}')

    if component_route == 'versioned':
        parent_steps = [child_reference]
    elif component_route == 'inline':
        parent_steps = [{
            'kind': 'custom', 'type': 'inline_sensitive_wrapper',
            'params': {'alias': '{{params.pin}}'},
            'steps': [{'type': 'if', 'config': {'then_steps': [
                {'type': action, 'config': {value_field: '{{params.alias}}'}},
                normal_text,
            ]}}],
        }]
    else:
        parent_steps = [{
            'kind': 'custom', 'type': 'inline_sensitive_wrapper',
            'params': {'alias': '{{params.pin}}'},
            'steps': [{'type': 'if', 'config': {'else_steps': [child_reference]}}],
        }]

    parent = _component(
        owner, project, name='sensitive forwarding parent',
        component_type='sensitive_forwarding_parent_fixture',
        schema={'properties': {'pin': {'type': 'string', 'sensitive': True}}},
        steps=parent_steps,
    )
    flow = [_reference(parent, pin='{{local.opaque}}')]
    case = AppTestCase.objects.create(
        name='synthetic sensitive forwarding case', project=project,
        created_by=member, ui_flow=flow,
        variables=[{'name': 'opaque', 'scope': 'local', 'value': 'synthetic-value'}],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)

    expanded, variables = prepare_scene_execution(case, execution)

    steps = list(_walk_steps(expanded))
    inputs = [
        step for step in steps
        if step.get('type') == action
        and step.get('config', {}).get(value_field) == '{{local.opaque}}'
    ]
    ordinary_text = [
        step for step in steps
        if step.get('type') == 'text'
        and step.get('config', {}).get('text') == '正常文案'
    ]
    assert len(inputs) == 1
    assert inputs[0]['config'].get('sensitive') is True
    assert len(ordinary_text) == 1
    assert 'sensitive' not in ordinary_text[0].get('config', {})

    runner = object.__new__(UiFlowRunner)
    runner._init_context(variables, {})
    runner._require_device = Mock(return_value=SimpleNamespace(snapshot=lambda **_kwargs: b'fake-screen'))
    protected_input = Mock()
    ordinary_input = Mock()
    runner._enter_sensitive_text = protected_input
    monkeypatch.setattr('apps.app_automation.runners.ui_flow_runner.airtest_text', ordinary_input)
    runner._dispatch_step = lambda step, step_action: (
        runner._action_input(step) if step_action == 'input' else runner._action_text(step)
    )
    runner._record_step_output = lambda step, output: None

    runner._execute_step(inputs[0])

    protected_input.assert_called_once_with('synthetic-value')
    ordinary_input.assert_not_called()


@pytest.mark.django_db
def test_shared_child_component_marks_only_the_sensitive_call_site(scene_context):
    owner, member, project = scene_context
    child = _component(
        owner, project, name='shared child fixture', component_type='shared_child_fixture',
        schema={'properties': {'payload': {'type': 'string'}}},
        steps=[{'type': 'input', 'config': {'value': '{{params.payload}}'}}],
    )
    parent = _component(
        owner, project, name='mixed-sensitivity parent', component_type='mixed_parent_fixture',
        schema={'properties': {
            'pin': {'type': 'string', 'sensitive': True},
            'label': {'type': 'string'},
        }},
        steps=[
            _reference(child, payload='{{params.pin}}'),
            _reference(child, payload='{{params.label}}'),
        ],
    )
    flow = [_reference(parent, pin='{{local.opaque}}', label='普通文案')]
    case = AppTestCase.objects.create(
        name='shared child call-site case', project=project,
        created_by=member, ui_flow=flow,
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)

    expanded, _ = prepare_scene_execution(case, execution)

    assert expanded == [
        {'type': 'input', 'config': {'value': '{{local.opaque}}', 'sensitive': True}},
        {'type': 'input', 'config': {'value': '普通文案'}},
    ]


@pytest.mark.django_db
def test_shared_child_is_sensitive_only_through_the_sensitive_parent_path(scene_context):
    owner, member, project = scene_context
    child = _component(
        owner, project, name='cross-parent shared child', component_type='cross_parent_child_fixture',
        schema={'properties': {'payload': {'type': 'string'}}},
        steps=[{'type': 'input', 'config': {'value': '{{params.payload}}'}}],
    )
    sensitive_parent = _component(
        owner, project, name='sensitive cross-parent wrapper',
        component_type='sensitive_cross_parent_fixture',
        schema={'properties': {'pin': {'type': 'string', 'sensitive': True}}},
        steps=[_reference(child, payload='{{params.pin}}')],
    )
    ordinary_parent = _component(
        owner, project, name='ordinary cross-parent wrapper',
        component_type='ordinary_cross_parent_fixture',
        schema={'properties': {'label': {'type': 'string'}}},
        steps=[_reference(child, payload='{{params.label}}')],
    )
    flow = [
        _reference(sensitive_parent, pin='{{local.opaque}}'),
        _reference(ordinary_parent, label='普通文案'),
    ]
    case = AppTestCase.objects.create(
        name='shared child through mixed parents', project=project,
        created_by=member, ui_flow=flow,
        variables=[{'name': 'opaque', 'scope': 'local', 'value': 'synthetic-value'}],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)

    expanded, _ = prepare_scene_execution(case, execution)

    assert expanded == [
        {'type': 'input', 'config': {'value': '{{local.opaque}}', 'sensitive': True}},
        {'type': 'input', 'config': {'value': '普通文案'}},
    ]


@pytest.mark.django_db
def test_sensitive_parent_taint_crosses_multiple_versioned_components(scene_context):
    owner, member, project = scene_context
    leaf = _component(
        owner, project, name='plain leaf fixture', component_type='plain_leaf_fixture',
        schema={'properties': {'payload': {'type': 'string'}}},
        steps=[{'type': 'if', 'then_steps': [
            {'type': 'input', 'config': {'value': '{{params.payload}}'}},
        ]}],
    )
    middle = _component(
        owner, project, name='plain forwarding fixture', component_type='plain_middle_fixture',
        schema={'properties': {'forwarded': {'type': 'string'}}},
        steps=[_reference(leaf, payload='{{params.forwarded}}')],
    )
    parent = _component(
        owner, project, name='multi-hop sensitive parent', component_type='multi_hop_parent_fixture',
        schema={'properties': {'pin': {'type': 'string', 'sensitive': True}}},
        steps=[_reference(middle, forwarded='{{params.pin}}')],
    )
    flow = [_reference(parent, pin='{{local.opaque}}')]
    case = AppTestCase.objects.create(
        name='multi-hop sensitive forwarding case', project=project,
        created_by=member, ui_flow=flow,
        variables=[{'name': 'opaque', 'scope': 'local', 'value': 'synthetic-value'}],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)

    expanded, _ = prepare_scene_execution(case, execution)

    input_step = next(step for step in _walk_steps(expanded) if step.get('type') == 'input')
    assert input_step == {
        'type': 'input', 'config': {'value': '{{local.opaque}}', 'sensitive': True},
    }


@pytest.mark.django_db
@pytest.mark.parametrize(('action', 'unused_field'), [
    ('input', 'text'),
    ('text', 'value'),
])
def test_explicit_sensitive_component_rejects_literal_in_unused_input_field(
    scene_context, action, unused_field,
):
    owner, member, project = scene_context
    private_value = f'synthetic-private-unused-{unused_field}'
    value_field = 'value' if action == 'input' else 'text'
    step = {
        'type': action,
        'config': {'sensitive': True, value_field: '{{params.payload}}'},
        unused_field: private_value,
    }
    component = _component(
        owner, project, name='explicit sensitive input fixture',
        component_type='explicit_sensitive_unused_field_fixture',
        schema={'properties': {'payload': {'type': 'string'}}}, steps=[step],
    )
    flow = [_reference(component, payload='{{local.opaque}}')]

    response = _post_case(member, project, flow)

    assert response.status_code == 400
    assert private_value not in str(response.data)

    case = AppTestCase.objects.create(
        name='legacy explicit sensitive component case', project=project,
        created_by=member, ui_flow=flow,
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    with pytest.raises(ScenePreparationError) as exc_info:
        prepare_scene_execution(case, execution)
    assert private_value not in str(exc_info.value)


@pytest.mark.parametrize(('action', 'unused_field'), [
    ('input', 'text'),
    ('text', 'value'),
])
def test_sensitive_scene_flow_rejects_literal_in_unused_conflicting_field(action, unused_field):
    private_value = f'synthetic-private-flow-unused-{unused_field}'
    value_field = 'value' if action == 'input' else 'text'
    step = {
        'type': action,
        'config': {'sensitive': True, value_field: '{{local.opaque}}'},
        unused_field: private_value,
    }

    with pytest.raises(SensitiveFlowError) as exc_info:
        protect_sensitive_inputs([step], [])

    assert private_value not in str(exc_info.value)
