"""Asserted API requests must carry their real assertion verdict into unified results."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from apps.api_testing.models import ApiCollection, ApiProject, ApiRequest
from apps.executions.adapters import ApiExecutorAdapter
from apps.executions.models import ExecutionJob, ExecutionSourceBinding
from apps.executions.source_workers import execute_api_source
from apps.projects.models import Project
from apps.users.models import User


@pytest.fixture
def assertion_target():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            status = int(self.path.removeprefix('/'))
            payload = b'{"marker":"actual HTTP response"}'
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('http_status', 'expected_status', 'expected_result'),
    [(500, 200, 'failed'), (200, 201, 'failed'), (200, 200, 'passed'), (500, None, 'passed')],
)
def test_direct_api_worker_finalizes_using_real_assertion_verdict(
    settings, assertion_target, http_status, expected_status, expected_result,
):
    settings.PERF_ALLOW_PRIVATE_TARGETS = True
    actor = User.objects.create_user(username='api-request-assertion-owner', password='fixture-only')
    project = Project.objects.create(name='Assertion verdict', owner=actor)
    native = ApiProject.objects.create(
        name='Native assertion verdict', owner=actor, project_type='HTTP', status='IN_PROGRESS',
    )
    collection = ApiCollection.objects.create(project=native, name='Real HTTP')
    request = ApiRequest.objects.create(
        collection=collection, name='Assert real status', method='GET',
        url=f'{assertion_target}/{http_status}', created_by=actor,
        assertions=[{'type': 'status_code', 'expected': expected_status}] if expected_status else [],
    )
    source_id = f'api:request:{request.pk}'
    ExecutionSourceBinding.objects.create(
        project=project, source_type='api', source_id=source_id,
        source_project_id=str(native.pk), created_by=actor,
    )
    job = ExecutionJob.objects.create(
        project=project, requested_by=actor, source_type='api', source_id=source_id,
    )
    receipt = ApiExecutorAdapter(dispatcher=lambda *_args: '').submit(job)

    execute_api_source.run(job.pk, receipt.attempt_id)

    job.refresh_from_db()
    assert job.final_status == expected_result
    assert job.final_attempt.status == expected_result
    assert job.final_result['status_code'] == http_status
    if expected_status is not None:
        assertion = job.final_result['assertions_results'][0]
        assert assertion['actual'] == http_status
        assert assertion['expected'] == expected_status
        assert assertion['passed'] is (expected_result == 'passed')
    else:
        assert job.final_result['assertions_results'] == []
    assert job.events.filter(event_type='run_finished').count() == 1
