import pytest

from apps.executions.failure_intelligence import analyze_job_failures, classify_failure
from apps.executions.result_import import import_results
from apps.projects.models import Project
from apps.testcases.models import TestCase
from apps.users.models import User


def test_failure_classification_is_deterministic():
    assert classify_failure('request timed out') == ('TIMEOUT', 'timeout')
    assert classify_failure('connection refused by upstream') == ('ENVIRONMENT', 'transport')
    assert classify_failure('AssertionError expected 200 actual 500') == ('PRODUCT', 'assertion')


@pytest.mark.django_db
def test_failure_analysis_clusters_imported_results():
    user = User.objects.create_user(username='failure-owner', password='pass')
    project = Project.objects.create(name='Failure Project', owner=user)
    TestCase.objects.create(project=project, external_ref='case.one', title='One', expected_result='ok', author=user)
    TestCase.objects.create(project=project, external_ref='case.two', title='Two', expected_result='ok', author=user)
    imported = import_results(
        project=project,
        payload=b'''<testsuite><testcase name="one"><properties><property name="external_ref" value="case.one"/></properties><failure message="expected 200 actual 500"/></testcase><testcase name="two"><properties><property name="external_ref" value="case.two"/></properties><failure message="expected 200 actual 500"/></testcase></testsuite>''',
        result_format='junit',
        external_run_id='failure-run',
        requested_by=user,
    )
    result = analyze_job_failures(imported.job)
    assert result['failure_count'] == 2
    assert result['clusters'][0]['category'] == 'PRODUCT'
    assert result['clusters'][0]['count'] == 2
