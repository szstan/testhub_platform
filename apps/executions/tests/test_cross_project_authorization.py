import pytest
from django.urls import include, path
from rest_framework.test import APIClient

from apps.defects.models import Defect
from apps.executions.models import (
    ExecutionBuild,
    ExecutionDataset,
    ExecutionJob,
    TestPlan as PlanModel,
    TestRun as RunModel,
)
from apps.projects.models import Project
from apps.requirement_analysis.models import (
    BusinessRequirement,
    RequirementAnalysis,
    RequirementDocument,
)
from apps.testcases.models import TestCase as CaseModel
from apps.users.models import User
from apps.versions.models import Version


urlpatterns = [path('api/executions/', include('apps.executions.urls'))]


@pytest.fixture(autouse=True)
def isolated_urls(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def execution_boundary_data(db):
    owner = User.objects.create_user(username='execution-boundary-owner', password='pass')
    outsider = User.objects.create_user(username='execution-boundary-outsider', password='pass')
    project = Project.objects.create(name='Execution Boundary Project', owner=owner)
    other_project = Project.objects.create(name='Other Execution Boundary Project', owner=outsider)

    version = Version.objects.create(name='boundary-v1', created_by=owner)
    version.projects.add(project)
    other_version = Version.objects.create(name='boundary-v2', created_by=outsider)
    other_version.projects.add(other_project)

    dataset = ExecutionDataset.objects.create(
        project=project,
        name='boundary-dataset',
        data=[{'value': 1}],
        created_by=owner,
    )
    other_dataset = ExecutionDataset.objects.create(
        project=other_project,
        name='other-boundary-dataset',
        data=[{'value': 2}],
        created_by=outsider,
    )
    build = ExecutionBuild.objects.create(
        project=project,
        version=version,
        name='boundary-build',
        build_number='boundary-1',
        created_by=owner,
    )

    testcase = CaseModel.objects.create(
        project=project,
        title='Boundary testcase',
        expected_result='works',
        author=owner,
    )
    other_testcase = CaseModel.objects.create(
        project=other_project,
        title='Other boundary testcase',
        expected_result='works',
        author=outsider,
    )
    plan = PlanModel.objects.create(name='Boundary plan', creator=owner)
    plan.projects.add(project)
    other_plan = PlanModel.objects.create(name='Other boundary plan', creator=outsider)
    other_plan.projects.add(other_project)
    test_run = RunModel.objects.create(
        name='Boundary run',
        test_plan=plan,
        project=project,
        version=version,
        assignee=owner,
        creator=owner,
    )
    other_test_run = RunModel.objects.create(
        name='Other boundary run',
        test_plan=other_plan,
        project=other_project,
        version=other_version,
        assignee=outsider,
        creator=outsider,
    )
    job = ExecutionJob.objects.create(
        project=project,
        version=version,
        build=build,
        source_type='api',
        requested_by=owner,
    )
    other_job = ExecutionJob.objects.create(
        project=other_project,
        version=other_version,
        source_type='api',
        requested_by=outsider,
    )

    document = RequirementDocument.objects.create(
        title='Boundary requirements',
        document_type='txt',
        uploaded_by=owner,
        project=project,
        file_size=0,
    )
    analysis = RequirementAnalysis.objects.create(document=document, requirements_count=1)
    requirement = BusinessRequirement.objects.create(
        analysis=analysis,
        requirement_id='REQ-BOUNDARY-1',
        requirement_name='Boundary requirement',
        requirement_type='functional',
        module='core',
        requirement_level='high',
        description='boundary',
        acceptance_criteria='works',
    )
    other_document = RequirementDocument.objects.create(
        title='Other boundary requirements',
        document_type='txt',
        uploaded_by=outsider,
        project=other_project,
        file_size=0,
    )
    other_analysis = RequirementAnalysis.objects.create(document=other_document, requirements_count=1)
    other_requirement = BusinessRequirement.objects.create(
        analysis=other_analysis,
        requirement_id='REQ-OTHER-BOUNDARY-1',
        requirement_name='Other boundary requirement',
        requirement_type='functional',
        module='core',
        requirement_level='high',
        description='boundary',
        acceptance_criteria='works',
    )
    defect = Defect.objects.create(
        title='Boundary defect',
        project=project,
        reporter=owner,
        related_testcase=testcase,
    )
    other_defect = Defect.objects.create(
        title='Other boundary defect',
        project=other_project,
        reporter=outsider,
        related_testcase=other_testcase,
    )
    return locals()


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def assert_denied(response):
    assert response.status_code in {400, 403}, response.data


@pytest.mark.django_db
def test_build_and_data_matrix_enforce_project_scope_on_create_and_update(execution_boundary_data):
    data = execution_boundary_data
    client = client_for(data['owner'])

    created = client.post(
        '/api/executions/builds/',
        {
            'project': data['project'].id,
            'version': data['version'].id,
            'name': 'same-project-build',
            'build_number': 'same-project-1',
        },
        format='json',
    )
    assert created.status_code == 201, created.data
    build_id = created.data['id']
    denied = client.patch(
        f'/api/executions/builds/{build_id}/',
        {'project': data['other_project'].id},
        format='json',
    )
    assert denied.status_code == 403, denied.data

    matrix = client.post(
        '/api/executions/data-matrices/',
        {
            'dataset': data['dataset'].id,
            'name': 'same-project-matrix',
            'columns': ['value'],
            'rows': [{'value': 1}],
            'enabled': True,
        },
        format='json',
    )
    assert matrix.status_code == 201, matrix.data
    matrix_id = matrix.data['id']
    denied = client.patch(
        f'/api/executions/data-matrices/{matrix_id}/',
        {'dataset': data['other_dataset'].id},
        format='json',
    )
    assert denied.status_code == 403, denied.data


def test_run_case_enforces_same_project_testcase_and_run(execution_boundary_data):
    data = execution_boundary_data
    client = client_for(data['owner'])

    valid = client.post(
        '/api/executions/run_cases/',
        {
            'test_run': data['test_run'].id,
            'testcase': data['testcase'].id,
            'status': 'untested',
            'priority': 'medium',
        },
        format='json',
    )
    assert valid.status_code == 201, valid.data

    mismatch = client.post(
        '/api/executions/run_cases/',
        {
            'test_run': data['test_run'].id,
            'testcase': data['other_testcase'].id,
            'status': 'untested',
            'priority': 'medium',
        },
        format='json',
    )
    assert mismatch.status_code == 400, mismatch.data

    inaccessible = client.post(
        '/api/executions/run_cases/',
        {
            'test_run': data['other_test_run'].id,
            'testcase': data['other_testcase'].id,
            'status': 'untested',
            'priority': 'medium',
        },
        format='json',
    )
    assert inaccessible.status_code == 403, inaccessible.data

    update = client.patch(
        f"/api/executions/run_cases/{valid.data['id']}/",
        {'testcase': data['other_testcase'].id},
        format='json',
    )
    assert update.status_code == 400, update.data


@pytest.mark.django_db
def test_traceability_links_enforce_same_project_on_create_and_update(execution_boundary_data):
    data = execution_boundary_data
    client = client_for(data['owner'])

    requirement_link = client.post(
        '/api/executions/requirement-testcase-links/',
        {'requirement': data['requirement'].id, 'test_case': data['other_testcase'].id},
        format='json',
    )
    assert requirement_link.status_code == 400, requirement_link.data

    requirement_link = client.post(
        '/api/executions/requirement-testcase-links/',
        {'requirement': data['requirement'].id, 'test_case': data['testcase'].id},
        format='json',
    )
    assert requirement_link.status_code == 201, requirement_link.data
    denied = client.patch(
        f"/api/executions/requirement-testcase-links/{requirement_link.data['id']}/",
        {'test_case': data['other_testcase'].id},
        format='json',
    )
    assert denied.status_code == 400, denied.data

    case_defect = client.post(
        '/api/executions/testcase-defect-links/',
        {'test_case': data['testcase'].id, 'defect': data['other_defect'].id},
        format='json',
    )
    assert case_defect.status_code == 400, case_defect.data

    case_defect = client.post(
        '/api/executions/testcase-defect-links/',
        {'test_case': data['testcase'].id, 'defect': data['defect'].id},
        format='json',
    )
    assert case_defect.status_code == 201, case_defect.data
    denied = client.patch(
        f"/api/executions/testcase-defect-links/{case_defect.data['id']}/",
        {'defect': data['other_defect'].id},
        format='json',
    )
    assert denied.status_code == 400, denied.data

    execution_requirement = client.post(
        '/api/executions/execution-requirement-links/',
        {'job': data['job'].id, 'requirement': data['other_requirement'].id},
        format='json',
    )
    assert execution_requirement.status_code == 400, execution_requirement.data

    execution_requirement = client.post(
        '/api/executions/execution-requirement-links/',
        {'job': data['job'].id, 'requirement': data['requirement'].id},
        format='json',
    )
    assert execution_requirement.status_code == 201, execution_requirement.data
    denied = client.patch(
        f"/api/executions/execution-requirement-links/{execution_requirement.data['id']}/",
        {'requirement': data['other_requirement'].id},
        format='json',
    )
    assert denied.status_code == 400, denied.data

    execution_defect = client.post(
        '/api/executions/execution-defect-links/',
        {'job': data['job'].id, 'defect': data['other_defect'].id},
        format='json',
    )
    assert execution_defect.status_code == 400, execution_defect.data

    execution_defect = client.post(
        '/api/executions/execution-defect-links/',
        {'job': data['job'].id, 'defect': data['defect'].id},
        format='json',
    )
    assert execution_defect.status_code == 201, execution_defect.data
    denied = client.patch(
        f"/api/executions/execution-defect-links/{execution_defect.data['id']}/",
        {'defect': data['other_defect'].id},
        format='json',
    )
    assert denied.status_code == 400, denied.data

    # A member/owner of the same project can still update a relation safely.
    valid_update = client.patch(
        f"/api/executions/execution-defect-links/{execution_defect.data['id']}/",
        {'relation_type': 'verified'},
        format='json',
    )
    assert valid_update.status_code == 200, valid_update.data


@pytest.mark.django_db
def test_dataset_and_plugin_writes_cannot_cross_project(execution_boundary_data):
    data = execution_boundary_data
    client = client_for(data['owner'])

    dataset = client.patch(
        f"/api/executions/datasets/{data['dataset'].id}/",
        {'project': data['other_project'].id},
        format='json',
    )
    assert dataset.status_code == 403, dataset.data

    plugin = client.post(
        '/api/executions/plugins/',
        {
            'project': data['other_project'].id,
            'name': 'cross-project-plugin',
            'version': '1.0.0',
            'kind': 'executor',
            'capabilities': [],
            'schema': {},
            'permissions': [],
            'enabled': True,
        },
        format='json',
    )
    assert plugin.status_code == 403, plugin.data

    global_plugin = client.post(
        '/api/executions/plugins/',
        {
            'name': 'global-plugin',
            'version': '1.0.0',
            'kind': 'executor',
            'capabilities': [],
            'schema': {},
            'permissions': [],
            'enabled': True,
        },
        format='json',
    )
    assert global_plugin.status_code == 403, global_plugin.data
