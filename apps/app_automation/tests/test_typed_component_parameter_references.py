import pytest

from apps.app_automation.component_templates import ComponentTemplateError, expand_steps
from apps.app_automation.models import AppProject, AppProjectComponent, AppTestCase, AppTestExecution
from apps.app_automation.scene_component_runtime import SceneComponentError, expand_project_flow
from apps.users.models import User


@pytest.mark.parametrize(
    ('parameter_type', 'reference'),
    [
        ('number', '{{local.attempts}}'),
        ('boolean', '${global.enabled}'),
        ('array', '{{local.payload.items}}'),
        ('object', '${global.payload}'),
    ],
)
def test_typed_component_parameters_defer_exact_scene_references_to_runner(parameter_type, reference):
    definition = {
        'project_id': 'p1',
        'type': 'typed_component',
        'version': '1',
        'schema': {'properties': {'value': {'type': parameter_type}}, 'required': ['value']},
        'steps': [{'type': 'consume', 'config': {'value': '{{params.value}}'}}],
    }

    result = expand_steps(
        [{
            'kind': 'custom', 'type': 'typed_component', 'version': '1',
            'params': {'value': reference},
        }],
        project_id='p1',
        resolve_template=lambda *_: definition,
    )

    assert result == [{'type': 'consume', 'config': {'value': reference}}]


@pytest.mark.parametrize(
    ('parameter_type', 'literal', 'expected', 'invalid'),
    [
        ('number', 2.5, 2.5, '2.5'),
        ('boolean', False, False, 0),
        ('array', [1, 'two'], [1, 'two'], {'item': 1}),
        ('object', {'enabled': False}, {'enabled': False}, [False]),
    ],
)
def test_typed_component_literals_keep_json_types_and_still_reject_mismatches(
    parameter_type, literal, expected, invalid,
):
    definition = {
        'project_id': 'p1',
        'type': 'typed_component',
        'version': '1',
        'schema': {'properties': {'value': {'type': parameter_type}}},
        'steps': [{'type': 'consume', 'config': {'value': '{{params.value}}'}}],
    }
    resolver = lambda *_: definition

    result = expand_steps(
        [{
            'kind': 'custom', 'type': 'typed_component', 'version': '1',
            'params': {'value': literal},
        }],
        project_id='p1', resolve_template=resolver,
    )
    assert result[0]['config']['value'] == expected
    assert type(result[0]['config']['value']) is type(expected)

    with pytest.raises(ComponentTemplateError, match='declared type'):
        expand_steps(
            [{
                'kind': 'custom', 'type': 'typed_component', 'version': '1',
                'params': {'value': invalid},
            }],
            project_id='p1', resolve_template=resolver,
        )


@pytest.mark.parametrize('value', [
    '{{params.missing}}',
    '${params.missing}',
    'prefix {{params.missing}}',
])
def test_component_parameter_values_cannot_forward_unresolved_params_references(value):
    definition = {
        'project_id': 'p1',
        'type': 'string_component',
        'version': '1',
        'schema': {'properties': {'value': {'type': 'string'}}},
        'steps': [{'type': 'consume', 'config': {'value': '{{params.value}}'}}],
    }

    with pytest.raises(ComponentTemplateError):
        expand_steps(
            [{
                'kind': 'custom', 'type': 'string_component', 'version': '1',
                'params': {'value': value},
            }],
            project_id='p1',
            resolve_template=lambda *_: definition,
        )


@pytest.mark.django_db
def test_sensitive_typed_parameter_requires_reference_and_expands_without_resolving_it():
    owner = User.objects.create_user(username='typed-secret-owner')
    project = AppProject.objects.create(name='typed secret project', owner=owner)
    AppProjectComponent.objects.create(
        project=project,
        name='typed secret component',
        type='typed_secret_component',
        version='1',
        schema={'properties': {'secret_limit': {'type': 'number', 'sensitive': True}}},
        steps=[{'type': 'consume', 'config': {'limit': '{{params.secret_limit}}'}}],
        created_by=owner,
    )
    reference = {
        'kind': 'custom', 'type': 'typed_secret_component', 'version': '1',
        'params': {'secret_limit': '${global.limit}'},
    }
    case = AppTestCase.objects.create(
        name='typed secret case', project=project, created_by=owner, ui_flow=[reference],
    )
    execution = AppTestExecution.objects.create(test_case=case, user=owner)

    expanded = expand_project_flow(execution, case, [reference])
    assert expanded == [{'type': 'consume', 'config': {'limit': '${global.limit}'}}]

    private_value = 'synthetic-private-number'
    with pytest.raises(SceneComponentError) as exc_info:
        expand_project_flow(execution, case, [{
            **reference,
            'params': {'secret_limit': private_value},
        }])
    assert private_value not in str(exc_info.value)
