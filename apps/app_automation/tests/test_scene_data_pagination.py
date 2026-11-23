import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.app_automation.models import AppDataSet, AppProject, AppProjectComponent
from apps.app_automation.views.scene_data_views import ProjectComponentViewSet, ProjectDataSetViewSet
from apps.users.models import User


@pytest.mark.django_db
def test_scene_data_endpoints_apply_bounded_project_pagination():
    owner = User.objects.create_user(username='scene-pagination-owner')
    other_owner = User.objects.create_user(username='scene-pagination-other')
    project = AppProject.objects.create(name='scene-pagination-project', owner=owner)
    other_project = AppProject.objects.create(name='scene-pagination-other-project', owner=other_owner)

    for index in range(101):
        AppDataSet.objects.create(project=project, name=f'dataset-{index:03}', version=1)
        AppProjectComponent.objects.create(
            project=project, name=f'component-{index:03}', type=f'component_{index:03}', version='1',
        )
    other_dataset = AppDataSet.objects.create(project=other_project, name='other-dataset', version=1)
    other_component = AppProjectComponent.objects.create(
        project=other_project, name='other-component', type='other_component', version='1',
    )

    factory = APIRequestFactory()

    def list_page(viewset, *, page=1, page_size=None):
        params = {'project': project.pk, 'page': page}
        if page_size is not None:
            params['page_size'] = page_size
        request = factory.get('/scene-data/', params)
        force_authenticate(request, user=owner)
        return viewset.as_view({'get': 'list'})(request)

    for viewset, excluded_id in (
        (ProjectDataSetViewSet, other_dataset.pk),
        (ProjectComponentViewSet, other_component.pk),
    ):
        first_default = list_page(viewset)
        second_default = list_page(viewset, page=2)
        assert first_default.status_code == 200
        assert first_default.data['count'] == 101
        assert len(first_default.data['results']) == 20
        assert first_default.data['next']
        assert len(second_default.data['results']) == 20

        first_large = list_page(viewset, page_size=1000)
        second_large = list_page(viewset, page=2, page_size=1000)
        assert len(first_large.data['results']) == 100
        assert first_large.data['next']
        assert len(second_large.data['results']) == 1
        assert second_large.data['next'] is None
        assert excluded_id not in {item['id'] for item in first_large.data['results']}
