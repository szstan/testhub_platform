import pytest

from apps.app_automation import scene_component_runtime as runtime
from apps.app_automation.models import (
    AppProject,
    AppProjectComponent,
    AppTestCase,
    AppTestExecution,
)
from apps.app_automation.scene_component_runtime import (
    SceneComponentError,
    expand_project_flow,
    validate_project_component_references,
)
from apps.users.models import User


@pytest.fixture
def component_context(db):
    owner = User.objects.create_user(username='component-budget-owner')
    member = User.objects.create_user(username='component-budget-member')
    project = AppProject.objects.create(name='component budget project', owner=owner)
    project.members.add(member)
    case = AppTestCase.objects.create(name='component budget case', project=project, created_by=owner)
    execution = AppTestExecution.objects.create(test_case=case, user=member)
    return owner, member, project, case, execution


def _component(owner, project, *, component_type, schema, steps):
    return AppProjectComponent.objects.create(
        project=project,
        name=component_type,
        type=component_type,
        version='1',
        schema=schema,
        default_config={},
        steps=steps,
        created_by=owner,
    )


def _reference(component):
    return {'kind': 'custom', 'type': component.type, 'version': component.version}


def _large_metadata_schema():
    return {'properties': {'unused': {'type': 'string', 'description': 'x' * 760_000}}}


@pytest.mark.django_db
def test_validation_rejects_repeated_near_limit_source_before_reprocessing(component_context, monkeypatch):
    owner, member, project, _, _ = component_context
    component = _component(
        owner, project, component_type='large_metadata_component',
        schema=_large_metadata_schema(), steps=[{'type': 'wait'}],
    )
    flow = [_reference(component) for _ in range(8)]
    original_expand = runtime.expand_steps
    calls = []

    def counted_expand(*args, **kwargs):
        calls.append(args[0])
        return original_expand(*args, **kwargs)

    monkeypatch.setattr(runtime, 'expand_steps', counted_expand)

    with pytest.raises(SceneComponentError, match='大小限制'):
        validate_project_component_references(flow, project, member)

    assert len(calls) == 1
    calls.clear()
    assert validate_project_component_references([_reference(component)], project, member)
    assert len(calls) == 1


@pytest.mark.django_db
def test_validation_charges_repeated_nested_component_sources(component_context, monkeypatch):
    owner, member, project, _, _ = component_context
    child = _component(
        owner, project, component_type='large_nested_child',
        schema=_large_metadata_schema(), steps=[{'type': 'wait'}],
    )
    parent = _component(
        owner, project, component_type='nested_large_parent', schema={},
        steps=[_reference(child), _reference(child)],
    )
    original_expand = runtime.expand_steps
    calls = []

    def counted_expand(*args, **kwargs):
        calls.append(args[0])
        return original_expand(*args, **kwargs)

    monkeypatch.setattr(runtime, 'expand_steps', counted_expand)

    with pytest.raises(SceneComponentError, match='大小限制'):
        validate_project_component_references([_reference(parent)], project, member)

    assert calls == []


@pytest.mark.django_db
def test_execution_charges_repeated_nested_cached_sources(component_context, monkeypatch):
    owner, member, project, case, execution = component_context
    child = _component(
        owner, project, component_type='runtime_nested_large_child',
        schema={}, steps=[{'type': 'wait'}],
    )
    parent = _component(
        owner, project, component_type='runtime_nested_large_parent', schema={},
        steps=[_reference(child), _reference(child)],
    )
    flow = [_reference(parent)]
    contracts = validate_project_component_references(flow, project, member)

    child.schema = _large_metadata_schema()
    child.save(update_fields=['schema'])
    monkeypatch.setattr(runtime, 'validate_project_component_references', lambda *_args: contracts)

    original_expand = runtime.expand_steps
    calls = []

    def counted_expand(*args, **kwargs):
        calls.append(args[0])
        return original_expand(*args, **kwargs)

    monkeypatch.setattr(runtime, 'expand_steps', counted_expand)

    with pytest.raises(SceneComponentError, match='大小限制'):
        expand_project_flow(execution, case, flow)

    assert calls == []


@pytest.mark.django_db
def test_execution_rejects_repeated_near_limit_cached_source(component_context, monkeypatch):
    owner, member, project, case, execution = component_context
    component = _component(
        owner, project, component_type='runtime_large_metadata_component',
        schema={}, steps=[{'type': 'wait'}],
    )
    flow = [_reference(component) for _ in range(8)]
    contracts = validate_project_component_references(flow, project, member)

    component.schema = _large_metadata_schema()
    component.save(update_fields=['schema'])
    monkeypatch.setattr(runtime, 'validate_project_component_references', lambda *_args: contracts)

    original_expand = runtime.expand_steps
    calls = []

    def counted_expand(*args, **kwargs):
        calls.append(args[0])
        return original_expand(*args, **kwargs)

    monkeypatch.setattr(runtime, 'expand_steps', counted_expand)

    with pytest.raises(SceneComponentError, match='大小限制'):
        expand_project_flow(execution, case, flow)

    assert len(calls) == 1


@pytest.mark.django_db
def test_small_repeated_refs_remain_valid_and_component_resolution_stays_project_scoped(
    component_context,
):
    owner, member, project, case, execution = component_context
    local = _component(
        owner, project, component_type='scoped_component', schema={},
        steps=[{'type': 'wait', 'config': {'source': 'local'}}],
    )
    other_project = AppProject.objects.create(name='other budget project', owner=owner)
    other_project.members.add(member)
    _component(
        owner, other_project, component_type='scoped_component', schema={},
        steps=[{'type': 'wait', 'config': {'source': 'other'}}],
    )
    flow = [_reference(local) for _ in range(12)]

    expanded = expand_project_flow(execution, case, flow)

    assert expanded == [{'type': 'wait', 'config': {'source': 'local'}}] * 12
