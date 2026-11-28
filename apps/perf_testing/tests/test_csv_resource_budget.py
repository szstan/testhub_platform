"""Resource and transaction-boundary regressions for performance CSV files."""
import csv
import io
import tracemalloc
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.perf_testing.models import PerfDataFile, PerfProject
from apps.perf_testing.services.csv_inspect import CSVInspectionError, inspect_csv_stream
from apps.perf_testing.views import PerfDataFileViewSet


def inspect(raw, **overrides):
    limits = {
        'max_records': 100_000, 'max_columns': 256, 'max_cell_chars': 32_768,
        'max_record_chars': 65_536, 'max_physical_lines': 200_000,
    }
    limits.update(overrides)
    stream = io.BytesIO(raw)
    stream.seek(min(2, len(raw)))
    try:
        return inspect_csv_stream(stream, **limits)
    finally:
        assert not stream.closed
        assert stream.tell() == 0


def test_blank_record_amplification_has_constant_memory_and_no_global_csv_mutation():
    raw = b'user\nalice\n' + b'\n' * 60_000
    original_limit = csv.field_size_limit()
    tracemalloc.start()
    try:
        metadata = inspect(raw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert metadata == {'columns': ['user'], 'row_count': 1, 'total_rows': 60_001, 'rows': []}
    assert peak < 512 * 1024  # The old rows=list(reader(...)) allocated over 4 MiB.
    assert csv.field_size_limit() == original_limit


@pytest.mark.parametrize('raw,limits,message', [
    (b'user\nalice\n\n\n', {'max_records': 2}, '记录数'),
    (b'a,b,c\n1,2,3\n', {'max_columns': 2}, '列数'),
    (b'a\nabcdefghij\n', {'max_cell_chars': 5}, '单元格'),
    (b'a,b\n1234,5678\n', {'max_record_chars': 8}, '单条记录'),
    (b'a\n"one\ntwo\nthree"\n', {'max_physical_lines': 3}, '物理行数'),
    (b'', {}, '文件为空'),
    (b' , \n1,2\n', {}, '首行'),
    (b'user\n\n', {}, '没有数据行'),
])
def test_every_input_dimension_including_empty_records_is_bounded(raw, limits, message):
    with pytest.raises(CSVInspectionError, match=message):
        inspect(raw, **limits)


@pytest.mark.parametrize('encoding', ['utf-8-sig', 'gbk'])
def test_encoding_fallback_and_quoted_newlines_keep_metadata_and_bounded_preview(encoding):
    raw = ' 用户 ,备注\r\n张三,"第一行,逗号\r\n第二行"\r\n李四,"含""引号"\r\n\r\n'.encode(encoding)
    metadata = inspect(raw, preview_limit=1)
    assert metadata['columns'] == ['用户', '备注']
    assert metadata['row_count'] == 2
    assert metadata['total_rows'] == 3
    assert metadata['rows'] == [['张三', '第一行,逗号\r\n第二行']]


def test_quoted_commas_do_not_count_as_extra_columns():
    metadata = inspect(b'a,b\n1,"x,y,z"\n', max_columns=2, preview_limit=1)
    assert metadata['rows'] == [['1', 'x,y,z']]


def test_malformed_csv_rewinds_and_keeps_the_uploaded_file_open():
    with pytest.raises(csv.Error):
        inspect(b'a\n"unterminated\n')


@pytest.fixture
def data_file_client(db, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    user = get_user_model().objects.create_user(username='csv-budget-owner')
    project = PerfProject.objects.create(name='csv-budget-project', owner=user)
    data_file = PerfDataFile.objects.create(
        project=project, name='users', file_type='CSV',
        file=SimpleUploadedFile('users.csv', b'user\nalice\n'),
        columns=['user'], row_count=1, uploaded_by=user,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client, project, data_file


def test_create_replace_and_preview_all_apply_the_same_record_budget(data_file_client):
    client, project, data_file = data_file_client
    payload = b'user\nalice\n\n\n'
    with patch.object(PerfDataFileViewSet, 'MAX_CSV_RECORDS', 2):
        created = client.post('/api/perf-testing/data-files/', {
            'name': 'too many blanks', 'project': project.pk,
            'file': SimpleUploadedFile('large.csv', payload),
        }, format='multipart')
        replaced = client.patch(f'/api/perf-testing/data-files/{data_file.pk}/', {
            'file': SimpleUploadedFile('large.csv', payload),
        }, format='multipart')
        assert created.status_code == replaced.status_code == 400
        data_file.refresh_from_db()
        assert data_file.row_count == 1
        # A pre-existing stored file must not bypass bounded preview inspection.
        data_file.file.save('legacy.csv', SimpleUploadedFile('legacy.csv', payload), save=True)
        previewed = client.get(f'/api/perf-testing/data-files/{data_file.pk}/preview/')
        assert previewed.status_code == 400


@pytest.mark.parametrize('requested_type', [None, 'CSV'])
def test_replacement_parses_before_global_lock_and_rechecks_the_current_type(data_file_client, requested_type):
    client, _, data_file = data_file_client
    order = []
    original = PerfDataFileViewSet._inspect_upload

    def parse(view, file_type, upload):
        order.append('parse')
        result = original(view, file_type, upload)
        # Simulate a concurrent, committed type change during parsing.
        PerfDataFile.objects.filter(pk=data_file.pk).update(file_type='JMX')
        return result

    with patch.object(PerfDataFileViewSet, '_inspect_upload', parse), patch(
        'apps.perf_testing.views.executor.lock_execution_admission',
        side_effect=lambda: order.append('lock'),
    ):
        payload = {'file': SimpleUploadedFile('replacement.csv', b'user\nbob\n')}
        if requested_type:
            payload['file_type'] = requested_type
        response = client.patch(f'/api/perf-testing/data-files/{data_file.pk}/', payload, format='multipart')
    assert response.status_code == 409
    assert order == ['parse', 'lock']
    data_file.refresh_from_db()
    assert data_file.row_count == 1
    assert data_file.file_type == 'JMX'


def test_intentional_type_change_with_a_new_validated_upload_is_still_allowed(data_file_client):
    client, _, data_file = data_file_client
    response = client.patch(f'/api/perf-testing/data-files/{data_file.pk}/', {
        'file_type': 'UPLOAD', 'file': SimpleUploadedFile('request-body.bin', b'arbitrary body'),
    }, format='multipart')
    assert response.status_code == 200
    data_file.refresh_from_db()
    assert data_file.file_type == 'UPLOAD'
    assert data_file.columns == []
    assert data_file.row_count == 0


def test_metadata_update_cannot_change_concurrent_type_without_a_new_upload(data_file_client):
    client, _, data_file = data_file_client
    original = PerfDataFileViewSet.get_object
    lookups = []

    def get_object(view):
        instance = original(view)
        lookups.append(instance.file_type)
        if len(lookups) == 1:
            PerfDataFile.objects.filter(pk=data_file.pk).update(file_type='JMX')
        return instance

    with patch.object(PerfDataFileViewSet, 'get_object', get_object):
        response = client.patch(f'/api/perf-testing/data-files/{data_file.pk}/', {
            'name': 'metadata only', 'file_type': 'CSV',
        }, format='multipart')
    assert response.status_code == 409
    data_file.refresh_from_db()
    assert data_file.file_type == 'JMX'


def test_replacement_rechecks_authorization_after_unlocked_parsing(data_file_client):
    client, project, data_file = data_file_client
    outsider = get_user_model().objects.create_user(username='csv-budget-outsider')
    original = PerfDataFileViewSet._inspect_upload

    def parse(view, file_type, upload):
        result = original(view, file_type, upload)
        PerfProject.objects.filter(pk=project.pk).update(owner=outsider)
        return result

    with patch.object(PerfDataFileViewSet, '_inspect_upload', parse):
        response = client.patch(f'/api/perf-testing/data-files/{data_file.pk}/', {
            'file': SimpleUploadedFile('replacement.csv', b'user\nbob\n'),
        }, format='multipart')
    assert response.status_code == 404
    data_file.refresh_from_db()
    assert data_file.row_count == 1
