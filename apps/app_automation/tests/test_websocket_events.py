from unittest import TestCase
from unittest.mock import MagicMock, patch

from apps.app_automation import tasks


class TestAppExecutionWebSocketEvents(TestCase):
    def test_send_execution_update_publishes_normalized_payload(self):
        channel_layer = MagicMock()
        sender = MagicMock()
        with patch.object(tasks, 'get_channel_layer', return_value=channel_layer), \
                patch.object(tasks, 'async_to_sync', return_value=sender):
            sent = tasks.send_execution_update(
                17,
                status='running',
                progress=45,
                message='step complete',
            )

        self.assertTrue(sent)
        sender.assert_called_once()
        payload = sender.call_args.args[1]
        self.assertEqual(payload['type'], 'execution_update')
        self.assertEqual(payload['execution_id'], 17)
        self.assertEqual(payload['status'], 'running')
        self.assertEqual(payload['progress'], 45)
        self.assertEqual(payload['message'], 'step complete')

    def test_send_execution_update_reports_missing_channel_layer(self):
        with patch.object(tasks, 'get_channel_layer', return_value=None), \
                patch.object(tasks.logger, 'warning') as warning:
            sent = tasks.send_execution_update(17, status='running', progress=0)

        self.assertFalse(sent)
        warning.assert_called_once()

    def test_send_execution_update_reports_publish_failure_without_error_details(self):
        channel_layer = MagicMock()
        with patch.object(tasks, 'get_channel_layer', return_value=channel_layer), \
                patch.object(tasks, 'async_to_sync', side_effect=RuntimeError('redis details')), \
                patch.object(tasks.logger, 'warning') as warning:
            sent = tasks.send_execution_update(17, status='running', progress=0)

        self.assertFalse(sent)
        warning.assert_called_once()
        self.assertNotIn('redis details', str(warning.call_args))
