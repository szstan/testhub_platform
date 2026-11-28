import pytest
from rest_framework.test import APIClient

from apps.api_testing.models import ApiProject
from apps.app_automation.models import AppProject
from apps.perf_testing.models import PerfProject
from apps.projects.models import Project, ProjectMember
from apps.quality_intelligence.models import QualityBaseline
from apps.ui_automation.models import UiProject
from apps.users.models import User


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def _baseline_payload(project, *, source_type='API', project_type='PROJECT', metric_key='latency'):
    return {
        'source_type': source_type,
        'project_type': project_type,
        'project_id': str(project.pk),
        'metric_key': metric_key,
        'value': 10,
    }


def _legacy_project(source_type, project_id, owner):
    if source_type == 'API':
        return ApiProject.objects.create(
            pk=project_id, name='Legacy API project', project_type='HTTP',
            status='IN_PROGRESS', owner=owner,
        )
    if source_type == 'UI':
        return UiProject.objects.create(
            pk=project_id, name='Legacy UI project', base_url='https://example.test', owner=owner,
        )
    if source_type == 'APP':
        return AppProject.objects.create(pk=project_id, name='Legacy APP project', owner=owner)
    return PerfProject.objects.create(pk=project_id, name='Legacy PERF project', owner=owner)


@pytest.mark.django_db
def test_unified_baseline_writes_require_project_writer_roles():
    owner = User.objects.create_user(username='baseline-project-owner')
    viewer = User.objects.create_user(username='baseline-project-viewer')
    writer = User.objects.create_user(username='baseline-project-writer')
    admin = User.objects.create_user(username='baseline-project-admin', is_staff=True)
    project = Project.objects.create(name='Role-protected baseline project', owner=owner)
    ProjectMember.objects.create(project=project, user=viewer, role='viewer')
    ProjectMember.objects.create(project=project, user=writer, role='developer')
    payload = _baseline_payload(project)

    created = _client(owner).post('/api/quality-intelligence/baselines/', payload, format='json')
    assert created.status_code == 201
    baseline_id = created.data['id']
    viewer_client = _client(viewer)
    assert viewer_client.get(f'/api/quality-intelligence/baselines/{baseline_id}/').status_code == 200
    assert viewer_client.post('/api/quality-intelligence/baselines/', payload, format='json').status_code == 404
    assert viewer_client.patch(
        f'/api/quality-intelligence/baselines/{baseline_id}/', {'value': 11}, format='json',
    ).status_code == 404
    assert viewer_client.delete(f'/api/quality-intelligence/baselines/{baseline_id}/').status_code == 404
    assert QualityBaseline.objects.filter(pk=baseline_id, value=10).exists()

    writer_created = _client(writer).post(
        '/api/quality-intelligence/baselines/',
        _baseline_payload(project, metric_key='writer-latency'),
        format='json',
    )
    assert writer_created.status_code == 201
    writer_id = writer_created.data['id']
    assert _client(writer).patch(
        f'/api/quality-intelligence/baselines/{writer_id}/', {'value': 12}, format='json',
    ).status_code == 200
    assert _client(writer).delete(f'/api/quality-intelligence/baselines/{writer_id}/').status_code == 204

    admin_created = _client(admin).post(
        '/api/quality-intelligence/baselines/',
        _baseline_payload(project, metric_key='admin-latency'),
        format='json',
    )
    assert admin_created.status_code == 201


@pytest.mark.django_db
@pytest.mark.parametrize('source_type', ['API', 'UI', 'APP', 'PERF'])
def test_legacy_baseline_authorization_uses_its_own_project_namespace(source_type):
    project_id = 61037
    unified_owner = User.objects.create_user(username=f'{source_type.lower()}-unified-owner')
    legacy_owner = User.objects.create_user(username=f'{source_type.lower()}-legacy-owner')
    legacy_writer = User.objects.create_user(username=f'{source_type.lower()}-legacy-writer')
    admin = User.objects.create_user(username=f'{source_type.lower()}-legacy-admin', is_staff=True)
    Project.objects.create(pk=project_id, name='Colliding unified project', owner=unified_owner)
    legacy_project = _legacy_project(source_type, project_id, legacy_owner)
    legacy_project.members.add(legacy_writer)

    payload = _baseline_payload(
        legacy_project, source_type=source_type, project_type=source_type,
        metric_key=f'{source_type.lower()}-latency',
    )
    assert _client(unified_owner).post(
        '/api/quality-intelligence/baselines/', payload, format='json',
    ).status_code == 404
    assert _client(legacy_writer).post(
        '/api/quality-intelligence/baselines/', payload, format='json',
    ).status_code == 201
    assert _client(legacy_owner).post(
        '/api/quality-intelligence/baselines/',
        {**payload, 'metric_key': f'{source_type.lower()}-owner-latency'},
        format='json',
    ).status_code == 201
    assert _client(admin).post(
        '/api/quality-intelligence/baselines/',
        {**payload, 'metric_key': f'{source_type.lower()}-admin-latency'},
        format='json',
    ).status_code == 201


@pytest.mark.django_db
def test_baseline_update_requires_write_access_to_source_and_target_projects():
    owner = User.objects.create_user(username='baseline-move-owner')
    writer = User.objects.create_user(username='baseline-move-writer')
    target_owner = User.objects.create_user(username='baseline-target-owner')
    source = Project.objects.create(name='Baseline source project', owner=owner)
    target = Project.objects.create(name='Baseline target project', owner=target_owner)
    ProjectMember.objects.create(project=source, user=writer, role='tester')
    baseline = QualityBaseline.objects.create(
        source_type='API', project_type='PROJECT', project_id=str(source.pk),
        metric_key='move-latency', value=10,
    )

    response = _client(writer).patch(
        f'/api/quality-intelligence/baselines/{baseline.pk}/',
        {'project_id': str(target.pk)}, format='json',
    )

    assert response.status_code == 404
    baseline.refresh_from_db()
    assert baseline.project_id == str(source.pk)
