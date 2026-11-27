import copy

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import (
    AppProject,
    AppProjectComponent,
    AppTestCase,
    AppTestExecution,
)
from apps.app_automation.scene_component_runtime import SceneComponentError, expand_project_flow
from apps.app_automation.views.test_case_views import AppTestCaseViewSet
from apps.users.models import User


@pytest.fixture
def scene_context(db):
    owner = User.objects.create_user(username='secret-fence-owner')
    member = User.objects.create_user(username='secret-fence-member')
    project = AppProject.objects.create(name='secret-fence-project', owner=owner)
    project.members.add(member)
    component = AppProjectComponent.objects.create(
        project=project,
        name='secret fence fixture',
        type='secret_fence_fixture',
        version='1',
        schema={
            'properties': {
                'declared_secret': {'type': 'string', 'sensitive': True},
                'consumed_token': {'type': 'string'},
            },
        },
        steps=[{
            'type': 'input',
            'config': {'sensitive': True, 'value': '{{params.consumed_token}}'},
        }],
        created_by=owner,
    )
    return owner, member, project, component


def _post_case(user, payload):
    request = APIRequestFactory().post('/app-test-cases/', payload, format='json')
    force_authenticate(request, user=user)
    return AppTestCaseViewSet.as_view({'post': 'create'})(request)


def _patch_case(user, case_id, payload):
    request = APIRequestFactory().patch(f'/app-test-cases/{case_id}/', payload, format='json')
    force_authenticate(request, user=user)
    return AppTestCaseViewSet.as_view({'patch': 'partial_update'})(request, pk=case_id)


def _reference(**params):
    return {
        'kind': 'custom',
        'type': 'secret_fence_fixture',
        'version': '1',
        'params': params,
    }


def _case_payload(project, flow):
    return {'name': 'synthetic secret fence case', 'project': project.pk, 'ui_flow': flow}


def _forwarding_component(owner, project, child, inner_field):
    inner_reference = {
        'kind': 'custom', 'type': child.type, 'version': child.version,
    }
    inner_reference[inner_field] = {'consumed_token': '{{params.forwarded_value}}'}
    return AppProjectComponent.objects.create(
        project=project,
        name=f'parent fixture {inner_field}',
        type=f'secret_fence_parent_{inner_field}',
        version='1',
        schema={'properties': {'forwarded_value': {'type': 'string'}}},
        steps=[inner_reference],
        created_by=owner,
    )


@pytest.mark.django_db
@pytest.mark.parametrize('schema_flag', ['secret', 'writeOnly'])
def test_case_api_and_runtime_reject_secret_and_write_only_schema_flags(scene_context, schema_flag):
    owner, member, project, _ = scene_context
    component = AppProjectComponent.objects.create(
        project=project,
        name=f'{schema_flag} marker fixture',
        type=f'secret_fence_{schema_flag.lower()}_marker',
        version='1',
        schema={'properties': {'opaque': {'type': 'string', schema_flag: True}}},
        steps=[{'type': 'wait'}],
        created_by=owner,
    )
    private_value = f'synthetic-{schema_flag}-private-value'
    flow = [{
        'kind': 'custom', 'type': component.type, 'version': component.version,
        'params': {'opaque': private_value},
    }]

    response = _post_case(member, _case_payload(project, flow))

    assert response.status_code == 400
    assert private_value not in str(response.data)
    assert not AppTestCase.objects.filter(project=project).exists()

    case = AppTestCase.objects.create(
        name='schema flag legacy case', project=project, created_by=member, ui_flow=flow,
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    with pytest.raises(SceneComponentError) as exc_info:
        expand_project_flow(execution, case, flow)
    assert private_value not in str(exc_info.value)


@pytest.mark.django_db
@pytest.mark.parametrize('parameter', ['declared_secret', 'consumed_token'])
def test_case_api_rejects_sensitive_component_literals_without_echoing(scene_context, parameter):
    _, member, project, _ = scene_context
    private_value = f'synthetic-private-{parameter}'

    response = _post_case(member, _case_payload(project, [_reference(**{parameter: private_value})]))

    assert response.status_code == 400
    assert private_value not in str(response.data)
    assert not AppTestCase.objects.filter(project=project).exists()


@pytest.mark.django_db
def test_case_api_checks_unused_config_even_when_params_are_safe(scene_context):
    _, member, project, _ = scene_context
    private_value = 'synthetic-private-unused-config'
    ref = _reference(declared_secret='{{local.password}}')
    ref['config'] = {'declared_secret': private_value}

    response = _post_case(member, _case_payload(project, [ref]))

    assert response.status_code == 400
    assert private_value not in str(response.data)
    assert not AppTestCase.objects.filter(project=project).exists()


@pytest.mark.django_db
def test_case_api_accepts_exact_scene_variable_references_in_component_params(scene_context):
    _, member, project, _ = scene_context
    flow = [_reference(
        declared_secret='{{password}}',
        consumed_token='${global.access_token}',
    )]

    response = _post_case(member, _case_payload(project, flow))

    assert response.status_code == 201, response.data
    case = AppTestCase.objects.get(pk=response.data['id'])
    assert case.ui_flow == flow


@pytest.mark.django_db
def test_case_api_scopes_versioned_refs_to_the_case_project_and_user(scene_context):
    owner, member, project, _ = scene_context
    other_project = AppProject.objects.create(name='other secret-fence-project', owner=owner)
    AppProjectComponent.objects.create(
        project=other_project,
        name='same type elsewhere',
        type='secret_fence_fixture',
        version='2',
        schema={'properties': {'declared_secret': {'type': 'string', 'sensitive': True}}},
        steps=[{'type': 'wait'}],
        created_by=owner,
    )

    response = _post_case(member, _case_payload(project, [{
        'kind': 'custom', 'type': 'secret_fence_fixture', 'version': '2',
        'params': {'declared_secret': '{{local.password}}'},
    }]))

    assert response.status_code == 400
    assert not AppTestCase.objects.filter(project=project).exists()


@pytest.mark.django_db
def test_case_api_rejects_component_tokens_as_scene_variable_references(scene_context):
    _, member, project, _ = scene_context

    response = _post_case(member, _case_payload(project, [_reference(declared_secret='{{params.password}}')]))

    assert response.status_code == 400
    assert 'params.password' not in str(response.data)


@pytest.mark.django_db
def test_case_api_rejects_versioned_refs_when_creator_lacks_project_access(scene_context):
    _, _, project, _ = scene_context
    creator = User.objects.create_user(username='secret-fence-revoked-creator')
    case = AppTestCase.objects.create(
        name='created before membership revocation', project=project, created_by=creator,
        ui_flow=[{'type': 'wait'}],
    )

    response = _patch_case(creator, case.pk, {
        'ui_flow': [_reference(declared_secret='{{local.password}}')],
    })

    assert response.status_code == 400
    assert '项目组件配置无效或无权访问' in str(response.data)
    case.refresh_from_db()
    assert case.ui_flow == [{'type': 'wait'}]


@pytest.mark.django_db
@pytest.mark.parametrize('partial', [False, True])
def test_sensitive_input_in_ui_flow_envelope_is_validated_on_create_and_partial_update(
    scene_context, partial,
):
    _, member, project, _ = scene_context
    private_value = 'synthetic-private-envelope-input'
    envelope = {
        'steps': [{'type': 'input', 'config': {'sensitive': True, 'value': private_value}}],
        'runtime': {'stop_on_error': True},
    }
    if partial:
        case = AppTestCase.objects.create(
            name='existing envelope case', project=project, created_by=member,
            ui_flow={'steps': [{'type': 'wait'}], 'runtime': {}},
        )
        response = _patch_case(member, case.pk, {'ui_flow': envelope})
    else:
        response = _post_case(member, _case_payload(project, envelope))

    assert response.status_code == 400
    assert private_value not in str(response.data)
    if partial:
        case.refresh_from_db()
        assert case.ui_flow['steps'] == [{'type': 'wait'}]
    else:
        assert not AppTestCase.objects.filter(project=project).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('outer_field', ['params', 'config'])
@pytest.mark.parametrize('inner_field', ['params', 'config'])
def test_case_api_rejects_transitively_sensitive_component_literals(
    scene_context, outer_field, inner_field,
):
    owner, member, project, child = scene_context
    parent = _forwarding_component(owner, project, child, inner_field)
    private_value = f'synthetic-transitive-private-{outer_field}-{inner_field}'
    outer_reference = {
        'kind': 'custom', 'type': parent.type, 'version': parent.version,
    }
    outer_reference[outer_field] = {'forwarded_value': private_value}

    response = _post_case(member, _case_payload(project, [outer_reference]))

    assert response.status_code == 400
    assert private_value not in str(response.data)
    assert not AppTestCase.objects.filter(project=project).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('inner_field', ['params', 'config'])
def test_case_api_rejects_unused_outer_config_with_transitive_sensitive_parameter(
    scene_context, inner_field,
):
    owner, member, project, child = scene_context
    parent = _forwarding_component(owner, project, child, inner_field)
    private_value = f'synthetic-unused-transitive-config-{inner_field}'
    outer_reference = {
        'kind': 'custom', 'type': parent.type, 'version': parent.version,
        'params': {'forwarded_value': '{{local.forwarded}}'},
        'config': {'forwarded_value': private_value},
    }

    response = _post_case(member, _case_payload(project, [outer_reference]))

    assert response.status_code == 400
    assert private_value not in str(response.data)
    assert not AppTestCase.objects.filter(project=project).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('parameter', ['declared_secret', 'consumed_token'])
def test_runtime_expansion_rejects_saved_sensitive_component_literals(scene_context, parameter):
    _, member, project, _ = scene_context
    private_value = f'synthetic-runtime-private-{parameter}'
    case = AppTestCase.objects.create(
        name='legacy saved case', project=project, created_by=member,
        ui_flow=[_reference(**{parameter: private_value})],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)

    with pytest.raises(SceneComponentError) as exc_info:
        expand_project_flow(execution, case, case.ui_flow)

    assert private_value not in str(exc_info.value)


@pytest.mark.django_db
@pytest.mark.parametrize('inner_field', ['params', 'config'])
def test_runtime_expansion_rejects_unused_outer_config_with_transitive_sensitive_parameter(
    scene_context, inner_field,
):
    owner, member, project, child = scene_context
    parent = _forwarding_component(owner, project, child, inner_field)
    private_value = f'synthetic-runtime-unused-transitive-{inner_field}'
    outer_reference = {
        'kind': 'custom', 'type': parent.type, 'version': parent.version,
        'params': {'forwarded_value': '{{local.forwarded}}'},
        'config': {'forwarded_value': private_value},
    }
    case = AppTestCase.objects.create(
        name='unused outer config case', project=project, created_by=member,
        ui_flow=[outer_reference],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)

    with pytest.raises(SceneComponentError) as exc_info:
        expand_project_flow(execution, case, case.ui_flow)

    assert private_value not in str(exc_info.value)


@pytest.mark.django_db
@pytest.mark.parametrize('outer_field', ['params', 'config'])
@pytest.mark.parametrize('inner_field', ['params', 'config'])
def test_runtime_expansion_revalidates_transitive_sensitive_refs(
    scene_context, outer_field, inner_field,
):
    owner, member, project, child = scene_context
    parent = _forwarding_component(owner, project, child, inner_field)
    private_value = f'synthetic-runtime-transitive-{outer_field}-{inner_field}'
    outer_reference = {
        'kind': 'custom', 'type': parent.type, 'version': parent.version,
    }
    outer_reference[outer_field] = {'forwarded_value': private_value}
    case = AppTestCase.objects.create(
        name='nested legacy case', project=project, created_by=member,
        ui_flow=[outer_reference],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)

    with pytest.raises(SceneComponentError) as exc_info:
        expand_project_flow(execution, case, case.ui_flow)

    assert private_value not in str(exc_info.value)


@pytest.mark.django_db
def test_runtime_rejects_unsafe_expanded_sensitive_input_before_scene_preparation(
    scene_context, monkeypatch,
):
    _, member, project, _ = scene_context
    case = AppTestCase.objects.create(
        name='expansion output case', project=project, created_by=member,
        ui_flow=[_reference(consumed_token='{{local.value}}')],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    private_value = 'synthetic-expanded-private-input'
    from apps.app_automation import scene_component_runtime
    from apps.app_automation.scene_execution import ScenePreparationError, prepare_scene_execution

    monkeypatch.setattr(scene_component_runtime, 'expand_steps', lambda *args, **kwargs: [{
        'type': 'input', 'config': {'sensitive': True, 'value': private_value},
    }])

    with pytest.raises(ScenePreparationError) as exc_info:
        prepare_scene_execution(case, execution)

    assert private_value not in str(exc_info.value)


@pytest.mark.django_db
def test_runtime_expansion_bounds_transitive_component_cycles(scene_context):
    owner, member, project, _ = scene_context
    first = AppProjectComponent.objects.create(
        project=project, name='cycle first', type='secret_fence_cycle_first', version='1',
        schema={}, steps=[{'kind': 'custom', 'type': 'secret_fence_cycle_second', 'version': '1'}],
        created_by=owner,
    )
    AppProjectComponent.objects.create(
        project=project, name='cycle second', type='secret_fence_cycle_second', version='1',
        schema={}, steps=[{'kind': 'custom', 'type': first.type, 'version': first.version}],
        created_by=owner,
    )
    case = AppTestCase.objects.create(
        name='cycle case', project=project, created_by=member,
        ui_flow=[{'kind': 'custom', 'type': first.type, 'version': first.version}],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)

    with pytest.raises(SceneComponentError):
        expand_project_flow(execution, case, case.ui_flow)


@pytest.mark.django_db
def test_runtime_expansion_preserves_unversioned_legacy_custom_wrappers(scene_context):
    _, member, project, _ = scene_context
    legacy = {
        'kind': 'custom',
        'type': 'legacy_custom_fixture',
        'config': {'password': 'synthetic-legacy-value'},
        'steps': [{
            'type': 'input',
            'sensitive': True,
            'value': 'synthetic-legacy-inline-value',
        }],
    }
    original = copy.deepcopy(legacy)
    flow = [legacy, _reference(consumed_token='{{local.token}}')]
    case = AppTestCase.objects.create(
        name='legacy flow case', project=project, created_by=member, ui_flow=flow,
    )
    execution = AppTestExecution.objects.create(test_case=case, user=member)

    expanded = expand_project_flow(execution, case, case.ui_flow)

    assert expanded[0] == original
    assert expanded[1] == {
        'type': 'input',
        'config': {'sensitive': True, 'value': '{{local.token}}'},
    }
    assert case.ui_flow == flow
