import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.api_testing.models import ApiCollection, ApiProject, ApiRequest
from apps.monitor.models import NotificationChannel
from apps.perf_testing.models import (
    PerfComparisonReport,
    PerfDataFile,
    PerfExecution,
    PerfExecutionAdmission,
    PerfMetricSample,
    PerfProject,
    PerfRequestStat,
    PerfScenario,
    PerfScenarioStep,
    PerfScheduledTask,
)
from apps.perf_testing.serializers import PerfScheduledTaskSerializer
from apps.perf_testing.services.notifier import send_execution_notification
from apps.perf_testing.views import (
    MAX_RUN_LOG_TAIL_BYTES,
    PerfDataFileViewSet,
    _read_run_log_tail,
    resolve_script_ref,
)

User = get_user_model()


class PerfRegressionBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='perf-regression-owner')
        self.other = User.objects.create_user(username='perf-regression-other')
        self.project = PerfProject.objects.create(name='perf-project', owner=self.user)
        self.scenario = PerfScenario.objects.create(
            project=self.project, created_by=self.user, name='scenario', engine='BUILTIN')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def execution(self, project=None, scenario=None, execution_no='run'):
        project = project or self.project
        scenario = scenario or self.scenario
        return PerfExecution.objects.create(
            scenario=scenario, project=project, execution_no=execution_no,
            status='COMPLETED', executed_by=self.user)


class ApiRequestImportScopeTests(PerfRegressionBase):
    def setUp(self):
        super().setUp()
        self.api_project = ApiProject.objects.create(
            name='api-own', project_type='HTTP', status='IN_PROGRESS', owner=self.user)
        self.collection = ApiCollection.objects.create(
            name='own-collection', project=self.api_project)
        self.own_request = ApiRequest.objects.create(
            collection=self.collection, name='own-request', url='https://example.test/own',
            created_by=self.user)
        foreign_project = ApiProject.objects.create(
            name='api-foreign', project_type='HTTP', status='IN_PROGRESS', owner=self.other)
        foreign_collection = ApiCollection.objects.create(
            name='foreign-collection', project=foreign_project)
        self.foreign_request = ApiRequest.objects.create(
            collection=foreign_collection, name='foreign-request',
            url='https://example.test/foreign', created_by=self.other)

    def test_import_rejects_any_selected_http_request_outside_accessible_projects(self):
        response = self.client.post(
            f'/api/perf-testing/scenarios/{self.scenario.id}/import-from-api/',
            {'request_ids': [self.own_request.id, self.foreign_request.id]}, format='json')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.scenario.steps.count(), 0)

    def test_imports_request_from_accessible_api_project(self):
        response = self.client.post(
            f'/api/perf-testing/scenarios/{self.scenario.id}/import-from-api/',
            {'request_ids': [self.own_request.id]}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        step = self.scenario.steps.get()
        self.assertEqual(step.source_request_id, self.own_request.id)


class ComparisonIsolationAndBoundTests(PerfRegressionBase):
    def setUp(self):
        super().setUp()
        self.other_project = PerfProject.objects.create(
            name='second-perf-project', owner=self.user)
        self.other_scenario = PerfScenario.objects.create(
            project=self.other_project, created_by=self.user, name='other-scenario')
        self.foreign_execution = self.execution(
            self.other_project, self.other_scenario, 'foreign-run')
        self.runs = [self.execution(execution_no=f'run-{index}') for index in range(5)]
        for index, execution in enumerate(self.runs):
            PerfMetricSample.objects.bulk_create([
                PerfMetricSample(execution=execution, ts_offset=point, tps=index + point)
                for point in range(305)
            ], batch_size=200)
            PerfRequestStat.objects.bulk_create([
                PerfRequestStat(
                    execution=execution, step_name=f'step-{index}-{step}', method='GET',
                    url=f'/step/{step}', total=1, success=1, tps=1.0,
                )
                for step in range(3)
            ])

    def test_ad_hoc_and_saved_comparisons_reject_cross_project_runs(self):
        ids = [self.runs[0].id, self.foreign_execution.id]
        response = self.client.get('/api/perf-testing/executions/compare/',
                                   {'ids': ','.join(map(str, ids))})
        self.assertEqual(response.status_code, 400)
        for payload in (
            {'execution_ids': ids},
            {'execution_ids': ids, 'project_id': self.project.id},
        ):
            response = self.client.post('/api/perf-testing/comparison-reports/',
                                        payload, format='json')
            self.assertEqual(response.status_code, 400)
        self.assertEqual(PerfComparisonReport.objects.count(), 0)

    def test_report_comparison_batches_stats_and_serializes_only_bounded_samples(self):
        payload = {'execution_ids': [execution.id for execution in self.runs]}
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post('/api/perf-testing/comparison-reports/',
                                        payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertLessEqual(len(queries), 12, [query['sql'] for query in queries])
        report = PerfComparisonReport.objects.get(pk=response.data['id'])
        self.assertEqual(len(report.snapshot['step_comparison']), 15)
        self.assertTrue(all(len(row['samples']) == 300
                            for row in report.snapshot['executions']))


class ScheduledNotificationOwnershipTests(PerfRegressionBase):
    def setUp(self):
        super().setUp()
        self.project.members.add(self.other)
        self.scenario = self.scenario
        self.own_channel = NotificationChannel.objects.create(
            name='perf-own-channel', type='EMAIL', config={'to': 'owner@example.test'},
            created_by=self.user)
        self.other_channel = NotificationChannel.objects.create(
            name='perf-other-channel', type='EMAIL', config={'to': 'other@example.test'},
            created_by=self.other)

    def _request(self, user):
        return SimpleNamespace(user=user)

    def test_schedule_save_rejects_foreign_channel_and_records_valid_owner(self):
        payload = {
            'scenario': self.scenario.id,
            'name': 'scheduled',
            'trigger_type': 'INTERVAL',
            'interval_minutes': 5,
            'notify_channels': [self.other_channel.id],
        }
        denied = PerfScheduledTaskSerializer(
            data=payload, context={'request': self._request(self.user)})
        self.assertFalse(denied.is_valid())
        self.assertIn('notify_channels', denied.errors)

        payload['notify_channels'] = [self.own_channel.id]
        allowed = PerfScheduledTaskSerializer(
            data=payload, context={'request': self._request(self.user)})
        self.assertTrue(allowed.is_valid(), allowed.errors)
        task = allowed.save()
        self.assertEqual(task.notify_channels_set_by_id, self.user.id)

    def test_update_atomically_rebinds_channels_to_the_configuring_actor(self):
        task = PerfScheduledTask.objects.create(
            scenario=self.scenario, name='channel-update', trigger_type='INTERVAL',
            interval_minutes=5, created_by=self.user,
            notify_channels=[self.own_channel.pk], notify_channels_set_by=self.user,
        )
        updated = PerfScheduledTaskSerializer(
            task, data={'notify_channels': [self.other_channel.pk]}, partial=True,
            context={'request': self._request(self.other)},
        )
        self.assertTrue(updated.is_valid(), updated.errors)
        updated.save()
        task.refresh_from_db()
        self.assertEqual(task.notify_channels_set_by_id, self.other.pk)
        self.assertEqual(task.notify_channels, [self.other_channel.pk])
        self.assertIsNotNone(task.next_run_at)
        denied = PerfScheduledTaskSerializer(
            task, data={'notify_channels': [self.own_channel.pk]}, partial=True,
            context={'request': self._request(self.other)},
        )
        self.assertFalse(denied.is_valid())
        with patch('apps.monitor.utils.notifiers.send_via_channel', return_value=True) as send:
            result = send_execution_notification(
                self.execution(), task.notify_channels, owner_id=task.notify_channels_set_by_id,
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(send.call_args.args[0].pk, self.other_channel.pk)

    def test_delivery_rejects_deactivated_channel_actor(self):
        execution = self.execution()
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        with patch('apps.monitor.utils.notifiers.send_via_channel') as send:
            result = send_execution_notification(
                execution, [self.own_channel.pk], owner_id=self.user.pk,
            )
        self.assertEqual(result, [])
        send.assert_not_called()

    def test_send_rechecks_current_channel_owner(self):
        execution = self.execution()
        self.own_channel.created_by = self.other
        self.own_channel.save(update_fields=['created_by'])
        with patch('apps.monitor.utils.notifiers.send_via_channel') as send:
            result = send_execution_notification(
                execution, [self.own_channel.id], owner_id=self.user.id)
        self.assertEqual(result, [])
        send.assert_not_called()


class DataFileReplacementTests(PerfRegressionBase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.data_file = PerfDataFile.objects.create(
            project=self.project, name='users', file_type='CSV',
            file=SimpleUploadedFile('users.csv', b'user\nalice\n'),
            columns=['user'], row_count=1, uploaded_by=self.user)

    def _patch(self, upload, **fields):
        return self.client.patch(
            f'/api/perf-testing/data-files/{self.data_file.id}/',
            {'file': upload, **fields}, format='multipart')

    def test_csv_replacement_reuses_create_parse_validation_and_updates_metadata(self):
        old_name = self.data_file.file.name
        invalid = self._patch(SimpleUploadedFile('bad.csv', b'user\n'))
        self.assertEqual(invalid.status_code, 400)
        self.data_file.refresh_from_db()
        self.assertEqual(self.data_file.file.name, old_name)
        self.assertEqual(self.data_file.row_count, 1)

        valid = self._patch(SimpleUploadedFile('replacement.csv', b'user\nbob\ncarol\n'))
        self.assertEqual(valid.status_code, 200, valid.data)
        self.data_file.refresh_from_db()
        self.assertEqual(self.data_file.columns, ['user'])
        self.assertEqual(self.data_file.row_count, 2)
        self.assertNotEqual(self.data_file.file.name, old_name)

    def test_replacement_enforces_size_and_jmx_staff_rules(self):
        with patch.object(PerfDataFileViewSet, 'MAX_SIZE', 4):
            too_large = self._patch(SimpleUploadedFile('large.csv', b'12345'))
        self.assertEqual(too_large.status_code, 400)

        script = PerfDataFile.objects.create(
            project=self.project, name='script', file_type='JMX',
            file=SimpleUploadedFile('script.jmx', b'<jmeterTestPlan/>'),
            uploaded_by=self.user)
        denied = self.client.patch(
            f'/api/perf-testing/data-files/{script.id}/',
            {'file': SimpleUploadedFile('new.jmx', b'<bad/>')}, format='multipart')
        self.assertEqual(denied.status_code, 403)

        staff = User.objects.create_user(username='perf-script-validation-staff', is_staff=True)
        self.client.force_authenticate(user=staff)
        with patch('apps.perf_testing.services.jmx_inspect.inspect_jmx_bytes', return_value={
                'valid': False, 'error': 'invalid test plan'}):
            malformed = self.client.patch(
                f'/api/perf-testing/data-files/{script.id}/',
                {'file': SimpleUploadedFile('invalid.jmx', b'<bad/>')}, format='multipart')
        self.assertEqual(malformed.status_code, 400)

    def test_jmx_replacement_rechecks_active_execution_reference(self):
        staff = User.objects.create_user(username='perf-script-staff', is_staff=True)
        self.client.force_authenticate(user=staff)
        script = PerfDataFile.objects.create(
            project=self.project, name='script', file_type='JMX',
            file=SimpleUploadedFile('script.jmx', b'<jmeterTestPlan/>'),
            uploaded_by=staff)
        PerfExecution.objects.create(
            scenario=self.scenario, project=self.project, execution_no='active-script',
            status='RUNNING', script_ref={'mode': 'script', 'data_file_id': script.id})
        valid_meta = {'valid': True, 'sampler_count': 1, 'thread_groups': []}
        with patch('apps.perf_testing.services.jmx_inspect.inspect_jmx_bytes',
                   return_value=valid_meta), patch(
                       'apps.perf_testing.services.jmx_inspect.summarize', return_value={}):
            response = self.client.patch(
                f'/api/perf-testing/data-files/{script.id}/',
                {'file': SimpleUploadedFile('new.jmx', b'<jmeterTestPlan/>')},
                format='multipart')
        self.assertEqual(response.status_code, 400)


class BoundedSampleAndLogTests(PerfRegressionBase):
    def setUp(self):
        super().setUp()
        self.execution = self.execution(execution_no='sample-run')
        PerfMetricSample.objects.bulk_create([
            PerfMetricSample(execution=self.execution, ts_offset=point)
            for point in range(5001)
        ], batch_size=500)

    def test_samples_rejects_zero_and_caps_before_serialization(self):
        zero = self.client.get(
            f'/api/perf-testing/executions/{self.execution.id}/samples/?max_points=0')
        self.assertEqual(zero.status_code, 400)
        invalid = self.client.get(
            f'/api/perf-testing/executions/{self.execution.id}/samples/?max_points=nope')
        self.assertEqual(invalid.status_code, 400)

        seen = []

        class RecordingSerializer:
            def __init__(self, instance, many=False):
                points = list(instance)
                seen.append(len(points))
                self.data = [{'ts_offset': point.ts_offset} for point in points]

        with patch('apps.perf_testing.views.PerfMetricSampleSerializer', RecordingSerializer):
            response = self.client.get(
                f'/api/perf-testing/executions/{self.execution.id}/samples/?max_points=99999')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 5001)
        self.assertEqual(response.data['returned'], 5000)
        self.assertEqual(seen, [5000])

    def test_run_log_tail_reads_and_returns_a_bounded_byte_window(self):
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            path = stream.name
            stream.write(b'a' * (2 * MAX_RUN_LOG_TAIL_BYTES) + b'END')
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        result = _read_run_log_tail(path, 1)
        self.assertLessEqual(len(result.encode('utf-8')), MAX_RUN_LOG_TAIL_BYTES)
        self.assertTrue(result.endswith('END'))


@override_settings(PERF_MAX_CONCURRENT_EXECUTIONS=1)
class AtomicExecutionAdmissionTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.temp_media = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.temp_media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.user = User.objects.create_user(username='perf-admission-user')
        self.project = PerfProject.objects.create(name='admission-project', owner=self.user)
        PerfExecutionAdmission.objects.get_or_create(key='global')
        self.scenario = PerfScenario.objects.create(
            project=self.project, created_by=self.user, name='admission-scenario')
        self.second_scenario = PerfScenario.objects.create(
            project=self.project, created_by=self.user, name='second-admission-scenario')
        for scenario in (self.scenario, self.second_scenario):
            PerfScenarioStep.objects.create(
                scenario=scenario, name='request', url='https://example.test')

    def test_concurrent_starts_cannot_exceed_database_capacity(self):
        passing = {
            'passed': True,
            'errors': [],
            'warnings': [],
            'estimated': {},
        }

        def start(scenario):
            close_old_connections()
            try:
                return executor_start(scenario, self.user)
            finally:
                close_old_connections()

        from apps.perf_testing.services.executor import start_execution as executor_start
        with patch('apps.perf_testing.services.executor.preflight', return_value=passing), patch(
                       'apps.perf_testing.services.executor.spawn_execution',
                       side_effect=lambda execution: execution):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(start, (self.scenario, self.second_scenario)))

        admitted = [execution for execution, _check in results if execution is not None]
        rejected = [check for execution, check in results if execution is None]
        self.assertEqual(len(admitted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertFalse(rejected[0]['passed'])
        self.assertEqual(
            PerfExecution.objects.filter(status__in=PerfExecution.ACTIVE_STATUSES).count(), 1)

    def test_child_spawn_waits_for_outer_commit_and_is_discarded_on_rollback(self):
        from apps.perf_testing.services.executor import start_execution
        passing = {'passed': True, 'errors': [], 'warnings': [], 'estimated': {}}
        with (
            patch('apps.perf_testing.services.executor.preflight', return_value=passing),
            patch('apps.perf_testing.services.executor.spawn_execution') as spawn,
        ):
            with transaction.atomic():
                execution, check = start_execution(self.scenario, self.user)
                self.assertTrue(check['passed'])
                self.assertIsNotNone(execution.pk)
                spawn.assert_not_called()
            spawn.assert_called_once_with(execution)
            PerfExecution.objects.filter(pk=execution.pk).update(status='COMPLETED')
            spawn.reset_mock()
            with self.assertRaisesRegex(ValueError, 'rollback'):
                with transaction.atomic():
                    rolled_back, check = start_execution(self.second_scenario, self.user)
                    self.assertTrue(check['passed'])
                    spawn.assert_not_called()
                    raise ValueError('rollback')
            spawn.assert_not_called()
            self.assertFalse(PerfExecution.objects.filter(pk=rolled_back.pk).exists())

    def test_stale_resolved_script_is_refreshed_after_replacement_before_pending_reservation(self):
        from apps.perf_testing.services.executor import start_execution

        self.user.is_staff = True
        self.user.save(update_fields=['is_staff'])
        self.scenario.engine = 'JMETER'
        self.scenario.save(update_fields=['engine'])
        script = PerfDataFile.objects.create(
            project=self.project,
            name='script',
            file_type='JMX',
            file=SimpleUploadedFile('original.jmx', b'<jmeterTestPlan/>'),
            uploaded_by=self.user,
        )
        stale_ref, error = resolve_script_ref(
            self.scenario, {'mode': 'script', 'data_file_id': script.pk})
        self.assertIsNone(error)
        old_path = stale_ref['jmx_path']
        client = APIClient()
        client.force_authenticate(self.user)

        with patch.object(PerfDataFileViewSet, '_inspect_upload', return_value=(
                {'columns': [], 'row_count': 0, 'meta': {}}, None,
                {'valid': True, 'sampler_count': 1, 'thread_groups': []})):
            replaced = client.patch(
                f'/api/perf-testing/data-files/{script.pk}/',
                {'file': SimpleUploadedFile('replacement.jmx', b'<jmeterTestPlan/>')},
                format='multipart',
            )
        self.assertEqual(replaced.status_code, 200, replaced.data)
        script.refresh_from_db()
        new_path = script.file.path
        self.assertNotEqual(old_path, new_path)
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(new_path))

        passing = {'passed': True, 'errors': [], 'warnings': [], 'estimated': {}}

        def preflight(current_scenario, script_ref=None):
            self.assertEqual(current_scenario.pk, self.scenario.pk)
            self.assertEqual(script_ref['data_file_id'], script.pk)
            self.assertEqual(script_ref['jmx_path'], new_path)
            self.assertTrue(os.path.exists(script_ref['jmx_path']))
            return passing

        with (
            patch('apps.perf_testing.services.executor.preflight', side_effect=preflight),
            patch('apps.perf_testing.services.executor.spawn_execution') as spawn,
        ):
            execution, check = start_execution(
                self.scenario, self.user, script_ref=stale_ref)

        self.assertTrue(check['passed'])
        self.assertEqual(execution.status, 'PENDING')
        self.assertEqual(execution.script_ref['jmx_path'], new_path)
        self.assertFalse(os.path.exists(old_path))
        spawn.assert_called_once_with(execution)
