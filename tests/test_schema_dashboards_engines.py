from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class DashboardAndEngineSchemaTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def operation(self, path, method):
        self.assertIn(path, self.schema['paths'])
        operations = self.schema['paths'][path]
        self.assertIn(method.lower(), operations)
        return operations[method.lower()]

    def resolve(self, schema):
        while '$ref' in schema:
            name = schema['$ref'].rsplit('/', 1)[-1]
            schema = self.schema['components']['schemas'][name]
        return schema

    def properties(self, schema):
        schema = self.resolve(schema)
        result = dict(schema.get('properties', {}))
        for part in schema.get('allOf', []):
            result.update(self.properties(part))
        return result

    def enum_values(self, schema):
        schema = self.resolve(schema)
        if 'enum' in schema:
            return set(schema['enum'])
        for part in schema.get('allOf', []):
            values = self.enum_values(part)
            if values:
                return values
        return set()

    def response_schema(self, operation, code):
        response = operation['responses'][str(code)]
        self.assertIn('application/json', response.get('content', {}))
        return response['content']['application/json']['schema']

    def request_schema(self, operation):
        return operation['requestBody']['content']['application/json']['schema']

    def assert_statuses(self, operation, *codes):
        expected = {str(code) for code in codes}
        self.assertTrue(
            expected.issubset(operation['responses']),
            f"Missing declared responses: {expected - set(operation['responses'])}",
        )

    def test_api_and_ui_dashboards_have_typed_counts(self):
        api = self.operation('/api/api-testing/dashboard/stats/', 'get')
        api_fields = self.properties(self.response_schema(api, 200))
        self.assertEqual(
            set(api_fields),
            {'project_count', 'interface_count', 'suite_count', 'history_count'},
        )
        self.assertEqual(
            set(self.properties(self.response_schema(api, 401))),
            {'detail'},
        )
        self.assert_statuses(api, 200, 401)

        ui = self.operation('/api/ui-automation/dashboard/stats/', 'get')
        ui_fields = self.properties(self.response_schema(ui, 200))
        self.assertEqual(
            set(ui_fields),
            {'project_count', 'test_case_count', 'suite_count', 'execution_count'},
        )
        self.assert_statuses(ui, 200, 401)

    def test_app_config_methods_and_dashboard_envelopes(self):
        current = self.operation('/api/app-automation/config/current/', 'get')
        current_fields = self.properties(self.response_schema(current, 200))
        self.assertEqual(set(current_fields), {'success', 'data'})
        self.assertEqual(
            set(self.properties(current_fields['data'])),
            {'id', 'adb_path', 'created_at', 'updated_at'},
        )
        self.assert_statuses(current, 200, 401, 500)
        self.assertEqual(
            set(self.properties(self.response_schema(current, 500))),
            {'success', 'message'},
        )

        save = self.operation('/api/app-automation/config/save/', 'post')
        request_fields = self.properties(self.request_schema(save))
        self.assertIn('adb_path', request_fields)
        self.assertTrue(
            {'200', '400', '401', '403', '500'}.issubset(save['responses'])
        )
        self.assertEqual(
            set(self.properties(self.response_schema(save, 400))),
            {'success', 'message', 'errors'},
        )
        self.assertEqual(
            set(self.properties(self.response_schema(save, 403))),
            {'success', 'message'},
        )

        dashboard = self.operation('/api/app-automation/dashboard/statistics/', 'get')
        data_fields = self.properties(self.response_schema(dashboard, 200))
        self.assertEqual(set(data_fields), {'success', 'data'})
        dashboard_data = self.properties(data_fields['data'])
        self.assertEqual(
            set(dashboard_data),
            {'devices', 'test_cases', 'executions', 'recent_executions'},
        )
        self.assertEqual(
            set(self.properties(dashboard_data['executions'])),
            {'total', 'success', 'failed', 'pass_rate'},
        )
        recent = self.resolve(dashboard_data['recent_executions'])
        self.assertEqual(recent['type'], 'array')
        self.assertIn('items', recent)
        self.assert_statuses(dashboard, 200, 401, 500)

    def test_chat_action_documents_request_messages_and_errors(self):
        operation = self.operation('/api/assistant/chat/send_message/', 'post')
        request_fields = self.properties(self.request_schema(operation))
        self.assertEqual(set(request_fields), {'session_id', 'message'})
        self.assertEqual(request_fields['session_id']['maxLength'], 200)

        response_fields = self.properties(self.response_schema(operation, 200))
        self.assertEqual(
            set(response_fields),
            {'user_message', 'assistant_message', 'conversation_id'},
        )
        message_fields = self.properties(response_fields['user_message'])
        self.assertEqual(
            set(message_fields),
            {'id', 'role', 'content', 'conversation_id', 'message_id', 'created_at'},
        )
        role_schema = self.resolve(message_fields['role'])
        self.assertEqual(self.enum_values(role_schema), {'user', 'assistant'})
        for code in (400, 401, 404, 408, 502):
            self.assertIn(str(code), operation['responses'])
        self.assertEqual(
            set(self.properties(self.response_schema(operation, 400))),
            {'error'},
        )
        self.assert_statuses(operation, 200, 400, 401, 404, 408, 502)

    def test_monitor_dashboard_summary_time_series_and_scheduler_enum(self):
        dashboard = self.operation('/api/monitor/dashboard/', 'get')
        fields = self.properties(self.response_schema(dashboard, 200))
        self.assertEqual(set(fields), {'summary', 'by_type', 'trend', 'recent_failures'})
        self.assert_statuses(dashboard, 200, 401)
        summary = self.properties(fields['summary'])
        self.assertEqual(
            set(summary),
            {'total', 'enabled', 'disabled', 'up', 'down', 'unknown', 'availability', 'active_alerts'},
        )
        trend = self.resolve(fields['trend'])
        point = self.properties(trend['items'])
        self.assertEqual(set(point), {'ts', 'up', 'down', 'availability'})
        self.assertEqual(point['ts']['format'], 'date-time')
        types = self.resolve(fields['by_type'])
        type_schema = self.properties(types['items'])['type']
        self.assertEqual(
            self.enum_values(type_schema),
            {'LOGIN', 'HTTP', 'ONLINE', 'DOCKER', 'SL651'},
        )

        scheduler = self.operation('/api/monitor/dashboard/scheduler/', 'get')
        status_schema = self.properties(self.response_schema(scheduler, 200))['status']
        self.assertEqual(self.enum_values(status_schema), {'online', 'offline', 'unknown'})
        self.assert_statuses(scheduler, 200, 401)

    def test_engine_status_and_comparison_report_contracts(self):
        engine = self.operation('/api/perf-testing/engines/status/', 'get')
        engine_fields = self.properties(self.response_schema(engine, 200))
        self.assertEqual(set(engine_fields), {'engines', 'websocket', 'limits'})
        engines = self.resolve(engine_fields['engines'])
        engine_name = self.properties(engines['items'])['name']
        self.assertEqual(self.enum_values(engine_name), {'BUILTIN', 'LOCUST', 'JMETER'})
        self.assertEqual(
            set(self.properties(engine_fields['limits'])),
            {'max_concurrency', 'max_target_rps', 'max_duration', 'max_concurrent_executions'},
        )
        self.assert_statuses(engine, 200, 401)

        collection_path = '/api/perf-testing/comparison-reports/'
        listing = self.operation(collection_path, 'get')
        list_fields = self.properties(self.response_schema(listing, 200))
        self.assertEqual(set(list_fields), {'items', 'total'})
        self.assert_statuses(listing, 200, 401)
        self.assertTrue(any(
            parameter['name'] == 'project' and parameter['in'] == 'query'
            and parameter['schema']['type'] == 'integer'
            for parameter in listing['parameters']
        ))

        create = self.operation(collection_path, 'post')
        create_fields = self.properties(self.request_schema(create))
        self.assertEqual(
            set(create_fields),
            {'execution_ids', 'project_id', 'reference_execution_id', 'title', 'ai_analyze'},
        )
        self.assertEqual(create_fields['execution_ids']['minItems'], 2)
        self.assertEqual(create_fields['execution_ids']['maxItems'], 5)
        self.assertIn('201', create['responses'])
        self.assertIn('400', create['responses'])
        self.assertIn('404', create['responses'])
        self.assert_statuses(create, 201, 400, 401, 404)
        self.assertEqual(
            set(self.properties(self.response_schema(create, 400))),
            {'error'},
        )

        detail_path = next(
            path for path in self.schema['paths']
            if '/api/perf-testing/comparison-reports/{' in path
            and path.endswith('/')
        )
        detail = self.operation(detail_path, 'get')
        detail_fields = self.properties(self.response_schema(detail, 200))
        self.assert_statuses(detail, 200, 401, 404)
        self.assertEqual(
            set(self.properties(self.response_schema(detail, 404))),
            {'error'},
        )
        self.assertTrue(any(
            parameter['name'] == 'id' and parameter['in'] == 'path'
            and parameter['schema']['type'] == 'integer'
            for parameter in detail['parameters']
        ))
        self.assertIn('snapshot', detail_fields)
        snapshot = self.properties(detail_fields['snapshot'])
        self.assertEqual(
            set(snapshot),
            {'baseline_execution_no', 'reference_execution_id', 'metric_keys', 'executions', 'step_comparison'},
        )
        metric_keys = self.resolve(snapshot['metric_keys'])['items']
        self.assertEqual(
            self.enum_values(metric_keys),
            {'total_requests', 'tps', 'peak_tps', 'avg_rt', 'p90_rt', 'p95_rt', 'p99_rt', 'max_rt', 'error_rate'},
        )
        self.assertIn('delete', self.schema['paths'][detail_path])
        delete = self.operation(detail_path, 'delete')
        self.assertIn('204', delete['responses'])
        self.assert_statuses(delete, 204, 401, 404)
        self.assertNotIn('content', delete['responses']['204'])

    def test_ui_environment_actions_expose_browser_install_controls(self):
        check = self.operation(
            '/api/ui-automation/config/environment/check_environment/', 'get'
        )
        check_fields = self.properties(self.response_schema(check, 200))
        self.assertEqual(set(check_fields), {'os', 'system_browsers', 'playwright_browsers'})
        self.assert_statuses(check, 200, 401)
        system_browsers = self.resolve(check_fields['system_browsers'])
        system_name = self.properties(system_browsers['items'])['name']
        self.assertEqual(
            self.enum_values(system_name),
            {'chrome', 'firefox', 'edge', 'safari'},
        )
        playwright_browsers = self.resolve(check_fields['playwright_browsers'])
        playwright_name = self.properties(playwright_browsers['items'])['name']
        self.assertEqual(
            self.enum_values(playwright_name),
            {'chromium', 'firefox', 'webkit'},
        )

        install = self.operation(
            '/api/ui-automation/config/environment/install_driver/', 'post'
        )
        request_fields = self.properties(self.request_schema(install))
        self.assertEqual(set(request_fields), {'browser'})
        self.assertEqual(
            self.enum_values(request_fields['browser']),
            {'chromium', 'firefox', 'webkit'},
        )
        for code in (200, 400, 401, 403, 500):
            self.assertIn(str(code), install['responses'])
        self.assertEqual(
            set(self.properties(self.response_schema(install, 400))),
            {'error'},
        )
        self.assert_statuses(install, 200, 400, 401, 403, 500)
