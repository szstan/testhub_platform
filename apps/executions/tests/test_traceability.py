import pytest
from rest_framework.test import APIClient

from apps.defects.models import Defect
from apps.executions.models import (
    ExecutionBuild,
    ExecutionJob,
    ExecutionRequirementLink,
    RequirementTestCaseLink as RequirementTestCaseLinkModel,
    TestCaseDefectLink as CaseDefectLinkModel,
)
from apps.projects.models import Project
from apps.requirement_analysis.models import BusinessRequirement, RequirementAnalysis, RequirementDocument
from apps.testcases.models import TestCase as CaseModel
from apps.users.models import User
from apps.versions.models import Version


@pytest.fixture
def traceability_data(db):
    owner = User.objects.create_user(username='trace-owner', password='pass')
    outsider = User.objects.create_user(username='trace-outsider', password='pass')
    project = Project.objects.create(name='Trace Project', owner=owner)
    other_project = Project.objects.create(name='Other Trace Project', owner=outsider)
    version = Version.objects.create(name='v-trace', created_by=owner)
    version.projects.add(project)
    document = RequirementDocument.objects.create(
        title='Trace requirements',
        document_type='txt',
        uploaded_by=owner,
        project=project,
        file_size=0,
    )
    analysis = RequirementAnalysis.objects.create(document=document, requirements_count=1)
    requirement = BusinessRequirement.objects.create(
        analysis=analysis,
        requirement_id='REQ-TRACE-1',
        requirement_name='Trace requirement',
        requirement_type='functional',
        module='core',
        requirement_level='high',
        description='trace',
        acceptance_criteria='works',
    )
    testcase = CaseModel.objects.create(
        project=project,
        title='Trace testcase',
        expected_result='works',
        author=owner,
    )
    defect = Defect.objects.create(
        title='Trace defect',
        project=project,
        reporter=owner,
        related_testcase=testcase,
    )
    build = ExecutionBuild.objects.create(
        project=project,
        version=version,
        name='Trace build',
        build_number='build-1',
        commit_sha='abc123',
        created_by=owner,
    )
    job = ExecutionJob.objects.create(
        project=project,
        version=version,
        build=build,
        source_type='api',
        source_id='trace-suite',
        requested_by=owner,
    )
    return locals()


@pytest.mark.django_db
def test_traceability_links_and_coverage_are_project_scoped(traceability_data):
    data = traceability_data
    client = APIClient()
    client.force_authenticate(user=data['owner'])

    build_response = client.get('/api/executions/builds/')
    assert build_response.status_code == 200
    assert build_response.data['count'] == 1

    requirement_link = client.post(
        '/api/executions/requirement-testcase-links/',
        {'requirement': data['requirement'].id, 'test_case': data['testcase'].id},
        format='json',
    )
    assert requirement_link.status_code == 201
    assert RequirementTestCaseLinkModel.objects.count() == 1

    defect_link = client.post(
        '/api/executions/testcase-defect-links/',
        {'test_case': data['testcase'].id, 'defect': data['defect'].id},
        format='json',
    )
    assert defect_link.status_code == 201
    assert CaseDefectLinkModel.objects.count() == 1

    execution_requirement = client.post(
        '/api/executions/execution-requirement-links/',
        {'job': data['job'].id, 'requirement': data['requirement'].id},
        format='json',
    )
    assert execution_requirement.status_code == 201
    assert ExecutionRequirementLink.objects.count() == 1

    execution_defect = client.post(
        '/api/executions/execution-defect-links/',
        {'job': data['job'].id, 'defect': data['defect'].id},
        format='json',
    )
    assert execution_defect.status_code == 201

    coverage = client.get(f"/api/executions/builds/coverage/?project={data['project'].id}")
    assert coverage.status_code == 200
    assert coverage.data['total_requirements'] == 1
    assert coverage.data['covered_requirements'] == 1
    assert coverage.data['coverage_rate'] == 100.0

    client.force_authenticate(user=data['outsider'])
    assert client.get('/api/executions/builds/').data['count'] == 0
    assert client.get('/api/executions/requirement-testcase-links/').data['count'] == 0
    assert client.get(f"/api/executions/builds/coverage/?project={data['project'].id}").status_code == 403


@pytest.mark.django_db
def test_traceability_rejects_cross_project_links(traceability_data):
    data = traceability_data
    other_case = CaseModel.objects.create(

        project=data['other_project'],
        title='Other testcase',
        expected_result='works',
        author=data['outsider'],
    )
    client = APIClient()
    client.force_authenticate(user=data['owner'])
    response = client.post(
        '/api/executions/requirement-testcase-links/',
        {'requirement': data['requirement'].id, 'test_case': other_case.id},
        format='json',
    )
    assert response.status_code in {400, 403}
