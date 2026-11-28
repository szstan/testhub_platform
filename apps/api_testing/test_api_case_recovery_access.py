"""History reads recover only stale executions visible to the requesting user."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.api_testing.models import ApiCaseExecution, ApiProject, ApiTestCase
from apps.api_testing.test_execution_integration import graph


@pytest.mark.parametrize('detail', [False, True])
def test_history_recovers_visible_stale_executions_without_touching_other_projects(graph, detail):
    old = timezone.now() - timedelta(hours=1)
    case = ApiTestCase.objects.create(project=graph.project, name='Visible case', created_by=graph.owner)
    stale = ApiCaseExecution.objects.create(
        project=graph.project, test_case=case, test_case_name=case.name, executed_by=graph.owner,
        status='RUNNING', start_time=old, heartbeat_at=old,
    )
    outsider = get_user_model().objects.create_user(username='other-recovery-owner')
    foreign_project = ApiProject.objects.create(name='Private project', owner=outsider)
    foreign = ApiCaseExecution.objects.create(
        project=foreign_project, test_case_name='Private execution', executed_by=outsider,
        status='RUNNING', start_time=old, heartbeat_at=old,
    )
    url = f'/api/api-testing/case-executions/{stale.pk}/' if detail else f'/api/api-testing/test-cases/{case.pk}/executions/'
    with patch('apps.api_testing.utils.guarded_request') as transport:
        response = graph.client.get(url)
    assert response.status_code == 200, response.data
    record = response.data if detail else response.data['results'][0]
    assert record['status'] == 'FAILED'
    assert '心跳超时' in record['error_message']
    stale.refresh_from_db()
    foreign.refresh_from_db()
    assert stale.end_time is not None
    assert foreign.status == 'RUNNING'
    assert foreign.end_time is None
    transport.assert_not_called()
