from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from rest_framework.test import APIClient

from apps.app_automation import device_listing
from apps.app_automation.models import AppDevice
from apps.users.models import User


@pytest.fixture
def catalogue(db):
    admin = User.objects.create_user(username='device-roster-admin', is_staff=True)
    devices = AppDevice.objects.bulk_create([
        AppDevice(device_id=f'synthetic-roster-{index}', status='online')
        for index in range(101)
    ])
    client = APIClient()
    client.force_authenticate(user=admin)
    return admin, devices, client


def first_page(client):
    result = client.get('/api/app-automation/devices/', {
        'consistent': 1, 'ordering': 'id', 'page_size': 100,
    })
    assert result.status_code == 200, result.data
    return result


@pytest.mark.django_db
def test_roster_paginates_every_device_and_links_carry_the_snapshot(catalogue):
    _, devices, client = catalogue
    page = first_page(client)
    assert page.data['count'] == 101
    assert len(page.data['results']) == 100
    version = page.data['roster_version']
    assert len(version) == 64
    assert parse_qs(urlparse(page.data['next']).query)['expected_roster'] == [version]
    second = client.get(page.data['next'])
    assert second.status_code == 200, second.data
    assert second.data['roster_version'] == version
    assert [item['id'] for item in second.data['results']] == [devices[-1].pk]
    assert all('device_action_epoch' not in item for item in page.data['results'])


@pytest.mark.django_db
def test_balanced_delete_insert_between_pages_is_rejected(catalogue):
    _, devices, client = catalogue
    page = first_page(client)
    AppDevice.objects.filter(pk=devices[0].pk).delete()
    AppDevice.objects.create(device_id='synthetic-roster-replacement', status='online')
    assert AppDevice.objects.count() == 101
    second = client.get(page.data['next'])
    assert second.status_code == 409
    assert second.data['code'] == 'device_roster_changed'
    assert 'results' not in second.data


@pytest.mark.django_db
def test_mutation_after_roster_read_cannot_shift_the_current_page(catalogue):
    _, devices, client = catalogue
    original = device_listing._roster_version

    def version_then_change(user_id, ids):
        version = original(user_id, ids)
        AppDevice.objects.filter(pk=devices[0].pk).delete()
        AppDevice.objects.create(device_id='synthetic-mid-query-replacement', status='online')
        return version

    with patch.object(device_listing, '_roster_version', side_effect=version_then_change):
        page = client.get('/api/app-automation/devices/', {'consistent': 1, 'page_size': 100})
    assert page.status_code == 409
    assert page.data['code'] == 'device_roster_changed'


@pytest.mark.django_db
def test_expected_roster_is_bound_to_user_and_later_pages_require_it(catalogue):
    _, _, client = catalogue
    page = first_page(client)
    other_admin = User.objects.create_user(username='device-roster-other-admin', is_staff=True)
    client.force_authenticate(user=other_admin)
    second = client.get(page.data['next'])
    assert second.status_code == 409
    missing = client.get('/api/app-automation/devices/', {'consistent': 1, 'page': 2})
    assert missing.status_code == 400
    malformed = client.get('/api/app-automation/devices/', {
        'consistent': 1, 'expected_roster': 'synthetic-invalid-version',
    })
    assert malformed.status_code == 400
    assert 'synthetic-invalid-version' not in str(malformed.data)


@pytest.mark.django_db
def test_regular_pagination_filters_and_roster_limits_remain_bounded(catalogue, monkeypatch):
    _, _, client = catalogue
    regular = client.get('/api/app-automation/devices/', {'page_size': 1000})
    assert regular.status_code == 200
    assert len(regular.data['results']) == 100
    assert 'roster_version' not in regular.data
    filtered = client.get('/api/app-automation/devices/', {'consistent': 1, 'status': 'offline'})
    assert filtered.status_code == 200
    assert filtered.data['count'] == 0
    assert filtered.data['results'] == []
    monkeypatch.setattr(device_listing, '_MAX_ROSTER_DEVICES', 100)
    limited = client.get('/api/app-automation/devices/', {'consistent': 1})
    assert limited.status_code == 400
    assert limited.data['code'] == 'device_roster_limit'
