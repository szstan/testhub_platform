import copy

import pytest

from apps.app_automation.models import AppProject, AppProjectComponent, AppTestCase, AppTestExecution
from apps.app_automation.scene_component_runtime import SceneComponentError, expand_project_flow
from apps.users.models import User


@pytest.fixture
def component_context(db):
    owner = User.objects.create_user(username='component-runtime-owner')
    member = User.objects.create_user(username='component-runtime-member')
    project = AppProject.objects.create(name='component runtime', owner=owner)
    project.members.add(member)
    case = AppTestCase.objects.create(name='component case', project=project, created_by=owner)
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    component = AppProjectComponent.objects.create(
        project=project, name='fixture', type='fixture_component', version='1',
        schema={'properties': {'label': {'type': 'string'}}, 'required': ['label']},
        steps=[{'type': 'set_variable', 'config': {'name': 'choice', 'value': '{{params.label}}'}}],
        created_by=owner,
    )
    return owner, member, project, case, execution, component


def reference(label='picked'):
    return {'kind': 'custom', 'type': 'fixture_component', 'version': '1', 'params': {'label': label}}


@pytest.mark.django_db
def test_nested_project_reference_expands_but_legacy_custom_wrappers_remain(component_context):
    _, _, _, case, execution, _ = component_context
    flow = [
        {'type': 'if', 'config': {'then_steps': [reference()], 'else_steps': []}},
        {'kind': 'custom', 'type': 'legacy', 'config': {'steps': 'legacy parameter'},
         'steps': [{'type': 'wait', 'config': {'timeout': 0}}]},
    ]
    original = copy.deepcopy(flow)
    result = expand_project_flow(execution, case, flow)
    assert result[0]['config']['then_steps'][0]['config']['value'] == 'picked'
    assert result[1] == flow[1]
    assert flow == original


@pytest.mark.django_db
def test_project_and_membership_checked_again_when_worker_expands(component_context):
    owner, member, project, case, execution, _ = component_context
    project.members.remove(member)
    with pytest.raises(SceneComponentError, match='无权访问'):
        expand_project_flow(execution, case, [reference()])
    other = AppProject.objects.create(name='other project', owner=owner)
    other.members.add(member)
    case.project = other
    with pytest.raises(SceneComponentError, match='不存在'):
        expand_project_flow(execution, case, [reference()])


@pytest.mark.django_db
def test_aggregate_expansion_limits_apply_across_references(component_context, monkeypatch):
    from apps.app_automation import scene_component_runtime as module
    _, _, _, case, execution, component = component_context
    component.steps = [{'type': 'set_variable', 'config': {'name': 'data', 'value': 'x' * 2000}}]
    component.schema = {}
    component.save(update_fields=['steps', 'schema'])
    monkeypatch.setattr(module, '_MAX_BYTES', 7000)
    with pytest.raises(SceneComponentError, match='大小限制'):
        expand_project_flow(execution, case, [reference() for _ in range(4)])


@pytest.mark.django_db
def test_project_reference_requires_declared_parameters(component_context):
    _, _, _, case, execution, _ = component_context
    with pytest.raises(SceneComponentError, match='配置无效'):
        expand_project_flow(execution, case, [{'kind': 'custom', 'type': 'fixture_component', 'version': '1'}])
