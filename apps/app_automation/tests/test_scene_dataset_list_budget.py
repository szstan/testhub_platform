from types import SimpleNamespace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import AppDataSet, AppProject
from apps.app_automation.views.scene_data_views import (
    AppDataSetSerializer,
    ProjectDataSetViewSet,
    _DatasetArrayLength,
)
from apps.users.models import User


@pytest.mark.django_db
def test_dataset_list_counts_rows_without_fetching_the_json_payload():
    owner = User.objects.create_user(username='dataset-list-budget-owner')
    stranger = User.objects.create_user(username='dataset-list-budget-stranger')
    project = AppProject.objects.create(name='dataset-list-project', owner=owner)
    other_project = AppProject.objects.create(name='other-dataset-project', owner=stranger)
    data = [{'value': 'x' * 20000}, {'value': 'two'}]
    dataset = AppDataSet.objects.create(project=project, name='normal', rows=data, created_by=owner)
    malformed = AppDataSet.objects.create(project=project, name='legacy-object', rows={'a': 1}, created_by=owner)
    empty = AppDataSet.objects.create(project=project, name='empty', rows=[], created_by=owner)
    foreign = AppDataSet.objects.create(project=other_project, name='foreign', rows=data, created_by=stranger)

    factory = APIRequestFactory()
    raw_request = factory.get('/scene-datasets/', {'project': project.pk, 'page_size': 100})
    force_authenticate(raw_request, user=owner)
    view = ProjectDataSetViewSet()
    view.request = Request(raw_request)
    view.action = 'list'
    selected = view.get_queryset().get(pk=dataset.pk)
    assert 'rows' in selected.get_deferred_fields()
    with CaptureQueriesContext(connection) as queries:
        serialized = AppDataSetSerializer(selected, context={'view': view}).data
    assert not queries
    assert serialized['row_count'] == 2
    assert 'rows' not in serialized
    assert 'rows' in selected.get_deferred_fields()

    raw_request = factory.get('/scene-datasets/', {'project': project.pk, 'page_size': 100})
    force_authenticate(raw_request, user=owner)
    with CaptureQueriesContext(connection) as queries:
        response = ProjectDataSetViewSet.as_view({'get': 'list'})(raw_request)
        results = response.data['results']
    assert response.status_code == 200
    assert {item['id']: item['row_count'] for item in results} == {
        dataset.pk: 2, malformed.pk: 0, empty.pk: 0,
    }
    assert foreign.pk not in {item['id'] for item in results}
    assert all('rows' not in item for item in results)
    # One count query and one page query; no deferred-field query for each item.
    assert len(queries) == 2

    request = factory.get(f'/scene-datasets/{dataset.pk}/')
    force_authenticate(request, user=owner)
    detail = ProjectDataSetViewSet.as_view({'get': 'retrieve'})(request, pk=dataset.pk)
    assert detail.status_code == 200
    assert detail.data['rows'] == data
    assert detail.data['row_count'] == 2


@pytest.mark.django_db
def test_dataset_row_count_expression_generates_supported_database_functions():
    query = AppDataSet.objects.annotate(_dataset_row_count=_DatasetArrayLength('rows')).query
    compiler = query.get_compiler(connection=connection)
    expression = query.annotations['_dataset_row_count']
    for vendor, expected_type, expected_length, expected_array in (
        ('sqlite', 'JSON_TYPE', 'JSON_ARRAY_LENGTH', 'array'),
        ('mysql', 'JSON_TYPE', 'JSON_LENGTH', 'ARRAY'),
        ('postgresql', 'jsonb_typeof', 'jsonb_array_length', 'array'),
    ):
        sql, params = expression.as_sql(compiler, SimpleNamespace(vendor=vendor))
        assert expected_type in sql
        assert expected_length in sql
        assert params == (expected_array,)
