from django.core.checks import Tags, run_checks
from django.test import SimpleTestCase, override_settings


class PerfSettingsChecksTests(SimpleTestCase):
    def _errors(self):
        return [
            error for error in run_checks(tags=[Tags.compatibility])
            if error.id.startswith('perf_testing.')
        ]

    def test_default_performance_settings_are_valid(self):
        self.assertEqual(self._errors(), [])

    @override_settings(PERF_MAX_CONCURRENCY=0)
    def test_non_positive_capacity_limit_is_reported(self):
        errors = self._errors()
        self.assertTrue(any(error.id == 'perf_testing.E001' for error in errors))
        self.assertTrue(any('PERF_MAX_CONCURRENCY' in error.msg for error in errors))

    @override_settings(PERF_MAX_DURATION=None)
    def test_missing_duration_setting_is_reported(self):
        errors = self._errors()
        self.assertTrue(any(error.id == 'perf_testing.E001' for error in errors))
        self.assertTrue(any('PERF_MAX_DURATION' in error.msg for error in errors))

    @override_settings(PERF_LOAD_GEN_CPU_WARN=101)
    def test_cpu_threshold_outside_percentage_range_is_reported(self):
        errors = self._errors()
        self.assertTrue(any(error.id == 'perf_testing.E002' for error in errors))

    @override_settings(PERF_RETENTION_DAYS=30, PERF_ARTIFACT_RETENTION_DAYS=31)
    def test_artifact_retention_cannot_exceed_execution_retention(self):
        errors = self._errors()
        self.assertTrue(any(error.id == 'perf_testing.E003' for error in errors))
