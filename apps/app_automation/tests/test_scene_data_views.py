import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import AppDataSet, AppProject, AppProjectComponent
from apps.app_automation.views.scene_data_views import ProjectComponentViewSet, ProjectDataSetViewSet
from apps.users.models import User


@pytest.fixture
def scoped_projects(db):
    owner = User.objects.create_user(username='scene-data-owner')
    member = User.objects.create_user(username='scene-data-member')
    stranger = User.objects.create_user(username='scene-data-stranger')
    project = AppProject.objects.create(name='scene-data-project', owner=owner)
    project.members.add(member)
    other = AppProject.objects.create(name='other-project', owner=stranger)
    return owner, member, stranger, project, other


def call_view(viewset, mapping, method, user, data=None, *, pk=None, query=''):
    factory = APIRequestFactory()
    if method == 'post' and isinstance(data, dict) and 'file' in data:
        request = factory.post('/scene-data/' + query, data, format='multipart')
    else:
        request = getattr(factory, method)('/scene-data/' + query, data or {}, format='json')
    force_authenticate(request, user=user)
    kwargs = {'pk': pk} if pk is not None else {}
    return viewset.as_view(mapping)(request, **kwargs)


def upload_csv(user, project, csv_text, **extra):
    return call_view(ProjectDataSetViewSet, {'post': 'create'}, 'post', user, {
        'project': project.pk,
        'name': 'accounts',
        'version': '1',
        'file': SimpleUploadedFile('fixture.csv', csv_text.encode('utf-8'), content_type='text/csv'),
        **extra,
    })


def component_payload(project, **extra):
    return {
        'project': project.pk, 'name': 'Login fixture', 'type': 'project_login', 'version': '1',
        'schema': {'properties': {'user': {'type': 'string'}}, 'required': ['user']},
        'default_config': {},
        'steps': [{'type': 'input', 'config': {'value': '{{params.user}}'}}],
        **extra,
    }


@pytest.mark.django_db
def test_dataset_import_requires_project_membership_and_preserves_secret_references(scoped_projects):
    owner, member, stranger, project, _ = scoped_projects
    csv_text = 'username,password\nfixture,secret://qa_password\n'
    created = upload_csv(member, project, csv_text)
    assert created.status_code == 201
    dataset = AppDataSet.objects.get(pk=created.data['id'])
    assert dataset.rows == [{'username': 'fixture', 'password': 'secret://qa_password'}]
    assert dataset.sensitive_columns == ['password']
    assert upload_csv(stranger, project, csv_text, version='2').status_code == 403
    assert upload_csv(owner, project, csv_text).status_code == 409

    listed = call_view(ProjectDataSetViewSet, {'get': 'list'}, 'get', member,
                       query=f'?project={project.pk}')
    entries = listed.data.get('results', listed.data) if isinstance(listed.data, dict) else listed.data
    assert entries[0]['row_count'] == 1
    assert 'rows' not in entries[0]
    denied = call_view(ProjectDataSetViewSet, {'get': 'retrieve'}, 'get', stranger, pk=dataset.pk)
    assert denied.status_code == 404
    update = call_view(ProjectDataSetViewSet, {'get': 'retrieve'}, 'put', owner,
                       {'name': 'changed'}, pk=dataset.pk)
    assert update.status_code == 405


@pytest.mark.django_db
def test_dataset_rejects_plain_secrets_invalid_versions_and_bad_files_without_echo(scoped_projects):
    owner, _, _, project, _ = scoped_projects
    private = 'synthetic-password-never-return'
    response = upload_csv(owner, project, f'username,password\nfixture,{private}\n')
    assert response.status_code == 400
    assert private not in str(response.data)
    assert not AppDataSet.objects.exists()
    assert upload_csv(owner, project, 'name\nfixture\n', version='9' * 5000).status_code == 400
    assert upload_csv(owner, project, 'name\nfixture\n', sensitive_columns=json.dumps(['missing'])).status_code == 400


@pytest.mark.django_db
def test_component_versions_are_project_scoped_validated_and_immutable(scoped_projects):
    owner, member, stranger, project, other = scoped_projects
    created = call_view(ProjectComponentViewSet, {'post': 'create'}, 'post', member,
                        component_payload(project))
    assert created.status_code == 201
    component = AppProjectComponent.objects.get(pk=created.data['id'])
    assert component.created_by == member
    denied = call_view(ProjectComponentViewSet, {'get': 'retrieve'}, 'get', stranger, pk=component.pk)
    assert denied.status_code == 404
    assert call_view(ProjectComponentViewSet, {'post': 'create'}, 'post', stranger,
                     component_payload(project, version='2')).status_code == 400
    own_same_type = call_view(ProjectComponentViewSet, {'post': 'create'}, 'post', stranger,
                              component_payload(other))
    assert own_same_type.status_code == 201
    duplicate = call_view(ProjectComponentViewSet, {'post': 'create'}, 'post', owner,
                          component_payload(project))
    assert duplicate.status_code in (400, 409)
    update = call_view(ProjectComponentViewSet, {'get': 'retrieve'}, 'put', owner,
                       {'name': 'overwrite'}, pk=component.pk)
    assert update.status_code == 405


@pytest.mark.django_db
def test_component_rejects_invalid_nested_flow_and_plain_secret_default(scoped_projects):
    owner, _, _, project, _ = scoped_projects
    invalid = component_payload(project, steps=[{'type': 'if', 'config': {'then_steps': 'not a list'}}])
    assert call_view(ProjectComponentViewSet, {'post': 'create'}, 'post', owner, invalid).status_code == 400
    private = 'synthetic-private-value'
    payload = component_payload(project, default_config={'password': private})
    response = call_view(ProjectComponentViewSet, {'post': 'create'}, 'post', owner, payload)
    assert response.status_code == 400
    assert private not in str(response.data)


@pytest.mark.django_db
def test_component_rejects_required_schema_names_missing_from_properties(scoped_projects):
    owner, _, _, project, _ = scoped_projects
    payload = component_payload(project, schema={
        'properties': {'user': {'type': 'string'}},
        'required': ['missing_parameter'],
    })

    response = call_view(ProjectComponentViewSet, {'post': 'create'}, 'post', owner, payload)

    assert response.status_code == 400
    assert not AppProjectComponent.objects.filter(project=project).exists()
