"""Validate optional user email values emitted by the real project/version views."""
import pytest
from django.contrib.auth import get_user_model
from django.urls import include, path
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker, ValidationError
from rest_framework.test import APIClient

from apps.projects.models import Project
from apps.versions.models import Version


urlpatterns = [
    path('api/projects/', include('apps.projects.urls')),
    path('api/versions/', include('apps.versions.urls')),
]
pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]


def _email_schema(document, response, *fields):
    for field in (*fields, 'email'):
        while '$ref' in response:
            response = document['components']['schemas'][response['$ref'].rsplit('/', 1)[-1]]
        if field == 'items':
            response = response['items']
        else:
            response = response['properties'][field]
    return response


@pytest.mark.parametrize('email', ['', 'member@example.test'])
def test_project_member_and_version_creator_email_contracts(email):
    users = get_user_model()
    owner = users.objects.create_user(username='optional-email-owner', email=email)
    member = users.objects.create_user(username='optional-email-member', email=email)
    project = Project.objects.create(name='Optional email schema fixture', owner=owner)
    version = Version.objects.create(name='Optional email version', created_by=owner)
    version.projects.add(project)
    client = APIClient()
    client.force_authenticate(user=owner)
    document = SchemaGenerator(patterns=urlpatterns).get_schema(request=None, public=True)

    created = client.post(f'/api/projects/{project.pk}/members/add/', {'user_id': member.pk}, format='json')
    listed = client.get(f'/api/projects/{project.pk}/members/')
    versions = client.get(f'/api/versions/projects/{project.pk}/versions/')
    assert (created.status_code, listed.status_code, versions.status_code) == (201, 200, 200)
    samples = [
        ('/api/projects/{project_id}/members/add/', 'post', '201', ('user',), created.data['user']['email']),
        ('/api/projects/{project_id}/members/', 'get', '200', ('items',), listed.data[0]['email']),
        ('/api/versions/projects/{project_id}/versions/', 'get', '200', ('items', 'created_by'), versions.data[0]['created_by']['email']),
    ]
    for route, method, status, fields, value in samples:
        assert value == email
        response = document['paths'][route][method]['responses'][status]['content']['application/json']['schema']
        validator = Draft4Validator(_email_schema(document, response, *fields), format_checker=FormatChecker())
        validator.validate(value)
        with pytest.raises(ValidationError):
            validator.validate('invalid-email')
