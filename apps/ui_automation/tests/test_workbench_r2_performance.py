from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.ui_automation import readiness
from apps.ui_automation.models import (
    UiProject, Element, LocatorStrategy, TestCase as Case, TestCaseStep as Step,
    TestSuite as Suite, TestSuiteTestCase as SuiteCase,
)
from apps.ui_automation.playwright_engine import PlaywrightTestEngine
from apps.ui_automation.workbench import SubmissionValidationError, suite_request_snapshot


@pytest.fixture(autouse=True)
def empty_readiness_cache():
    readiness.clear_readiness_cache()
    yield
    readiness.clear_readiness_cache()


@pytest.fixture
def project_context(db):
    user = get_user_model().objects.create_user(username='r2-performance-owner')
    project = UiProject.objects.create(name='r2-performance', base_url='https://fixture.example.test', owner=user)
    client = APIClient()
    client.force_authenticate(user=user)
    return user, project, client


@pytest.mark.parametrize('with_steps', [True, False])
def test_suite_snapshot_batches_step_queries_and_preserves_order(project_context, with_steps):
    user, project, _client = project_context
    suite = Suite.objects.create(project=project, name='batched')
    cases = [Case.objects.create(project=project, name=f'case-{n}', created_by=user) for n in range(10)]
    SuiteCase.objects.bulk_create([SuiteCase(test_suite=suite, test_case=case, order=10-n) for n,case in enumerate(cases)])
    if with_steps:
        Step.objects.bulk_create([Step(test_case=case, step_number=number, action_type='wait', wait_time=number) for case in cases for number in (2,1)])
    with CaptureQueriesContext(connection) as queries:
        snapshot = suite_request_snapshot(suite, {'engine':'playwright','browser':'chrome','headless':True})
    assert len(queries) == 2
    assert [row['id'] for row in snapshot['cases']] == [case.pk for case in reversed(cases)]
    assert [step['step_number'] for step in snapshot['cases'][0]['steps']] == ([1,2] if with_steps else [])


def test_batch_snapshot_still_rejects_cross_project_element(project_context):
    user, project, _ = project_context
    other = UiProject.objects.create(name='other',base_url='https://other.example.test',owner=user)
    strategy = LocatorStrategy.objects.create(name='css')
    element = Element.objects.create(project=other,name='foreign',locator_strategy=strategy,locator_value='#foreign',created_by=user)
    case = Case.objects.create(project=project,name='case',created_by=user)
    Step.objects.create(test_case=case,step_number=1,action_type='click',element=element)
    suite = Suite.objects.create(project=project,name='suite')
    SuiteCase.objects.create(test_suite=suite,test_case=case)
    with pytest.raises(SubmissionValidationError):
        suite_request_snapshot(suite, {'engine':'playwright','browser':'chrome','headless':True})


def test_element_options_are_lightweight_paginated_and_scoped(project_context):
    user, project, client = project_context
    strategy = LocatorStrategy.objects.create(name='css')
    Element.objects.bulk_create([Element(project=project,name=f'element-{n}',locator_strategy=strategy,locator_value=f'#field-{n}',created_by=user) for n in range(101)])
    outsider = get_user_model().objects.create_user(username='r2-foreign-owner')
    other = UiProject.objects.create(name='private',base_url='https://private.example.test',owner=outsider)
    Element.objects.create(project=other,name='secret element',locator_strategy=strategy,locator_value='#secret',created_by=outsider)
    with CaptureQueriesContext(connection) as queries:
        first = client.get('/api/ui-automation/elements/', {'project':project.pk,'options_only':'true','page_size':100})
    assert first.status_code == 200
    assert first.data['count'] == 101
    assert len(first.data['results']) == 100
    assert len(queries) <= 3
    assert set(first.data['results'][0]) == {'id','project_id','name','page','element_type','locator_strategy_id','locator_value'}
    assert all('parent_element_id" =' not in row['sql'] for row in queries.captured_queries)
    second = client.get('/api/ui-automation/elements/', {'project':project.pk,'options_only':'true','page_size':100,'page':2})
    assert len(second.data['results']) == 1
    assert first.data['results'][-1]['id'] < second.data['results'][0]['id']
    denied = client.get('/api/ui-automation/elements/', {'project':other.pk,'options_only':'true'})
    assert denied.data['count'] == 0
    full = client.get('/api/ui-automation/elements/', {'project':project.pk})
    assert len(full.data['results']) == 20
    assert 'locator_strategy' in full.data['results'][0]
    detail = client.get(f"/api/ui-automation/elements/{first.data['results'][0]['id']}/", {'options_only':'true'})
    assert 'locator_strategy' in detail.data


def test_readiness_reuses_normalized_browser_and_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(readiness,'_clock',lambda:clock[0])
    probe = Mock(return_value=(True,'ready'))
    monkeypatch.setattr(PlaywrightTestEngine,'check_execution_environment_sync',probe)
    policy = {'allowed_private_cidrs':[]}
    assert readiness.cached_environment_check('playwright','chrome',policy) == (True,'ready')
    assert readiness.cached_environment_check('playwright','edge',policy) == (True,'ready')
    assert probe.call_count == 1
    probe.assert_called_with('chromium')
    clock[0] += readiness.READY_TTL_SECONDS + 1
    readiness.cached_environment_check('playwright','chrome',policy)
    assert probe.call_count == 2
    readiness.cached_environment_check('playwright','chrome',{'allowed_private_cidrs':['10.0.0.0/8']})
    assert probe.call_count == 3


def test_unavailable_readiness_expires_so_new_install_can_recover(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(readiness,'_clock',lambda:clock[0])
    probe = Mock(side_effect=[(False,'missing'),(True,'installed')])
    monkeypatch.setattr(PlaywrightTestEngine,'check_execution_environment_sync',probe)
    assert readiness.cached_environment_check('playwright','chrome',{}) == (False,'missing')
    assert readiness.cached_environment_check('playwright','chrome',{}) == (False,'missing')
    clock[0] += readiness.UNAVAILABLE_TTL_SECONDS + 1
    assert readiness.cached_environment_check('playwright','chrome',{}) == (True,'installed')
    assert probe.call_count == 2


def test_readiness_cache_is_bounded_and_exceptions_release_waiters(monkeypatch):
    probe = Mock(side_effect=RuntimeError('probe unavailable'))
    monkeypatch.setattr(PlaywrightTestEngine,'check_execution_environment_sync',probe)
    assert readiness.cached_environment_check('playwright','chrome',{})[0] is False
    assert not readiness._inflight
    probe.side_effect = None
    probe.return_value = (True,'ready')
    for number in range(readiness.MAX_CACHE_ENTRIES + 5):
        readiness.cached_environment_check('playwright','chrome',{'policy_version':number})
    assert len(readiness._cache) == readiness.MAX_CACHE_ENTRIES


def test_concurrent_readiness_misses_share_one_probe(monkeypatch):
    started, release = Event(), Event()
    def check(_browser):
        started.set()
        assert release.wait(3)
        return True,'ready'
    probe = Mock(side_effect=check)
    monkeypatch.setattr(PlaywrightTestEngine,'check_execution_environment_sync',probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(readiness.cached_environment_check,'playwright','chrome',{})
        assert started.wait(2)
        second = pool.submit(readiness.cached_environment_check,'playwright','edge',{})
        release.set()
        assert first.result(timeout=3) == second.result(timeout=3) == (True,'ready')
    assert probe.call_count == 1
    assert not readiness._inflight


def test_worker_environment_check_is_not_served_from_readiness_cache(monkeypatch):
    from apps.ui_automation.suite_runner import _check_environment
    probe = Mock(side_effect=[(True,'cached ready'),(False,'changed before execution')])
    monkeypatch.setattr(PlaywrightTestEngine,'check_execution_environment_sync',probe)
    assert readiness.cached_environment_check('playwright','chrome',{})[0] is True
    assert _check_environment('playwright','chrome') == (False,'changed before execution')
    assert probe.call_count == 2


def test_readiness_endpoint_reuses_probes_but_checks_url_and_permissions_each_time(project_context,monkeypatch):
    user, project, client = project_context
    guard = Mock(side_effect=lambda url:url)
    monkeypatch.setattr('apps.ui_automation.network_policy.validate_ui_outbound_url',guard)
    monkeypatch.setattr('apps.ui_automation.network_policy.get_ui_network_policy',lambda:{'allowed_private_cidrs':[]})
    probe = Mock(side_effect=lambda browser:(browser != 'webkit','unsupported' if browser == 'webkit' else 'ready'))
    monkeypatch.setattr(PlaywrightTestEngine,'check_execution_environment_sync',probe)
    selenium = Mock(return_value=(True,'ready'))
    monkeypatch.setattr('apps.ui_automation.selenium_engine.SeleniumTestEngine.check_execution_environment',selenium)
    url = f'/api/ui-automation/projects/{project.pk}/execution-readiness/'
    with override_settings(UI_SELENIUM_COMPATIBILITY=True):
        first = client.get(url)
        second = client.get(url)
    assert first.status_code == second.status_code == 200
    assert probe.call_count == 3  # Chromium shared by Chrome/Edge, Firefox, unavailable WebKit.
    assert selenium.call_count == 3
    guard.side_effect = ValueError('URL denied by current policy')
    with override_settings(UI_SELENIUM_COMPATIBILITY=False):
        denied = client.get(url)
    assert denied.data['base_url_ready'] is False
    assert next(row for row in denied.data['engines'] if row['value']=='selenium')['available'] is False
    assert selenium.call_count == 3
    assert guard.call_count == 3
    other = get_user_model().objects.create_user(username='new-project-owner')
    project.owner = other
    project.save(update_fields=['owner'])
    assert client.get(url).status_code == 404
