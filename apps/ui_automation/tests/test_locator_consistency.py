import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ui_automation import locator_validation
from apps.ui_automation.models import Element, LocatorStrategy, UiProject


@pytest.fixture
def locator_context(db):
    user = get_user_model().objects.create_user(username='locator-consistency')
    project = UiProject.objects.create(name='locator-consistency', base_url='https://safe.example.test', owner=user)
    strategy = LocatorStrategy.objects.create(name='css')
    element = Element.objects.create(
        project=project, name='saved', locator_strategy=strategy, locator_value='#saved',
        validation_status='VALID', validation_message='last verified', last_validated=timezone.now(), created_by=user,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client, element, strategy


def test_generic_element_writes_cannot_fabricate_validation_metadata(locator_context):
    client, element, strategy = locator_context
    response = client.post('/api/ui-automation/elements/', {
        'name': 'spoofed', 'project_id': element.project_id, 'locator_strategy_id': strategy.pk,
        'locator_value': '[[[', 'validation_status': 'VALID', 'validation_message': 'fake result',
        'last_validated': '2026-01-01T00:00:00Z',
    }, format='json')
    assert response.status_code == 201
    created = Element.objects.get(pk=response.data['id'])
    assert created.validation_status == 'UNKNOWN'
    assert created.last_validated is None
    assert created.validation_message != 'fake result'


def test_saving_a_different_locator_invalidates_its_previous_valid_result(locator_context):
    client, element, _ = locator_context
    response = client.patch(f'/api/ui-automation/elements/{element.pk}/', {
        'locator_value': '#changed', 'validation_status': 'VALID', 'validation_message': 'fake result',
    }, format='json')
    assert response.status_code == 200
    element.refresh_from_db()
    assert element.locator_value == '#changed'
    assert element.validation_status == 'UNKNOWN'
    assert element.validation_message == ''
    assert element.last_validated is None


def test_late_probe_cannot_mark_a_concurrently_changed_locator_valid(locator_context, monkeypatch):
    client, element, strategy = locator_context
    monkeypatch.setattr(locator_validation, 'validate_ui_outbound_url', lambda url: url)

    def probe(*_args):
        Element.objects.filter(pk=element.pk).update(locator_value='#changed-during-probe', validation_status='UNKNOWN')
        return 1, True, True

    monkeypatch.setattr(locator_validation, '_run_live_probe', probe)
    response = client.post(f'/api/ui-automation/elements/{element.pk}/validate_locator/', {
        'mode': 'live', 'expected_locator_value': '#saved', 'expected_locator_strategy_id': strategy.pk,
    }, format='json')
    assert response.status_code == 200
    assert response.data['is_valid'] is False
    assert response.data['validation_status'] == 'UNKNOWN'
    element.refresh_from_db()
    assert element.locator_value == '#changed-during-probe'
    assert element.validation_status == 'UNKNOWN'
