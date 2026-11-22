from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from .views import AnalyticsEventIngestView, AnalyticsEventThrottle


class AnalyticsEventThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.view = AnalyticsEventIngestView.as_view()

    def test_anonymous_ingest_is_rate_limited_by_client_ip(self):
        with patch.object(AnalyticsEventThrottle, 'rate', '1/minute'):
            with patch('apps.analytics.views.AnalyticsEvent.objects.bulk_create'):
                first_request = self.factory.post(
                    '/api/analytics/events/', {'event_name': 'page_view'}, format='json'
                )
                first_request.META['REMOTE_ADDR'] = '198.51.100.42'
                first_response = self.view(first_request)

                second_request = self.factory.post(
                    '/api/analytics/events/', {'event_name': 'page_view'}, format='json'
                )
                second_request.META['REMOTE_ADDR'] = '198.51.100.42'
                second_response = self.view(second_request)

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 429)
