from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from rest_framework.test import APIClient

from apps.perf_testing.models import PerfDataFile, PerfExecution, PerfProject, PerfScenario
from apps.perf_testing.services import executor
from apps.users.models import User


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def script_context(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    owner = User.objects.create_user(username='script-delete-owner', is_staff=True)
    project = PerfProject.objects.create(name='Script deletion project', owner=owner)
    scenario = PerfScenario.objects.create(project=project, name='Script scenario', created_by=owner)
    script = PerfDataFile.objects.create(
        project=project, name='Script', file_type='JMX',
        file=SimpleUploadedFile('plan.jmx', b'<jmeterTestPlan/>'), uploaded_by=owner,
    )
    client = APIClient()
    client.force_authenticate(owner)
    return owner, project, scenario, script, client


def test_script_storage_is_deleted_only_after_database_commit(script_context):
    _owner, _project, _scenario, script, client = script_context
    path = Path(script.file.path)
    actual_lock = executor.lock_execution_admission
    with patch.object(executor, 'lock_execution_admission', wraps=actual_lock) as lock:
        with transaction.atomic():
            response = client.delete(f'/api/perf-testing/data-files/{script.pk}/')
            assert response.status_code == 204, response.data
            assert not PerfDataFile.objects.filter(pk=script.pk).exists()
            assert path.exists()
        assert not path.exists()
    # Both delete and its deferred storage cleanup serialize against admission.
    assert lock.call_count == 2


def test_rollback_preserves_script_record_and_storage(script_context):
    _owner, _project, _scenario, script, client = script_context
    path = Path(script.file.path)
    with pytest.raises(ValueError, match='rollback'):
        with transaction.atomic():
            response = client.delete(f'/api/perf-testing/data-files/{script.pk}/')
            assert response.status_code == 204
            raise ValueError('rollback')
    assert PerfDataFile.objects.filter(pk=script.pk).exists()
    assert path.exists()


def test_pending_execution_blocks_deletion_after_shared_admission_lock(script_context):
    owner, project, scenario, script, client = script_context
    PerfExecution.objects.create(
        project=project, scenario=scenario, executed_by=owner, execution_no='pending-delete-guard',
        status='PENDING', script_ref={'mode': 'script', 'data_file_id': script.pk},
    )
    path = Path(script.file.path)
    actual_lock = executor.lock_execution_admission
    with patch.object(executor, 'lock_execution_admission', wraps=actual_lock) as lock:
        response = client.delete(f'/api/perf-testing/data-files/{script.pk}/')
    assert response.status_code == 400, response.data
    lock.assert_called_once_with()
    assert PerfDataFile.objects.filter(pk=script.pk).exists()
    assert path.exists()
