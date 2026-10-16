from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from apps.app_automation.views.execution_views import AppTestExecutionViewSet


class AppWebSocketStatusTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = AppTestExecutionViewSet()

    @patch('channels.layers.get_channel_layer')
    def test_asgi_proxy_request_reports_websocket_available(self, get_channel_layer):
        get_channel_layer.return_value = object()
        request = self.factory.get('/api/app-automation/executions/ws_status/')
        request.scope = {
            'type': 'http',
            'server': ('127.0.0.1', 18002),
        }
        request.META['SERVER_SOFTWARE'] = 'nginx'

        response = self.view.ws_status(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['websocket'])

    @patch('channels.layers.get_channel_layer')
    def test_wsgi_request_reports_websocket_unavailable(self, get_channel_layer):
        get_channel_layer.return_value = object()
        request = self.factory.get('/api/app-automation/executions/ws_status/')

        response = self.view.ws_status(request)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['websocket'])

    @patch('channels.layers.get_channel_layer', return_value=None)
    def test_asgi_request_without_channel_layer_reports_unavailable(self, _get_channel_layer):
        request = self.factory.get('/api/app-automation/executions/ws_status/')
        request.scope = {'type': 'http'}

        response = self.view.ws_status(request)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['websocket'])
