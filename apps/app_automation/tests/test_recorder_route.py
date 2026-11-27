from django.urls import resolve

from apps.app_automation.views.recorder_lease_views import RecorderLeaseView
from apps.app_automation.views.recorder_views import RecordInteractionView


def test_record_interaction_route_uses_the_hardened_recorder_view():
    match = resolve('/api/app-automation/devices/17/record-interaction/')

    assert match.url_name == 'app-device-record-interaction'
    assert match.func.view_class is RecordInteractionView


def test_recorder_lease_route_uses_the_token_fenced_view():
    match = resolve('/api/app-automation/devices/17/recorder-lease/')

    assert match.url_name == 'app-device-recorder-lease'
    assert match.func.view_class is RecorderLeaseView
