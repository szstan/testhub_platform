from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.ui_automation import locator_validation
from apps.ui_automation.models import Element, LocatorStrategy, UiProject

User = get_user_model()


def _element(*, strategy='css', value='#submit', unique=True):
    return SimpleNamespace(
        locator_strategy=SimpleNamespace(name=strategy),
        locator_value=value,
        project=SimpleNamespace(base_url='https://base.example.test/'),
        is_unique=unique,
    )


class _FakeLocator:
    first = None

    def __init__(self, count=1, *, error=None):
        self.match_count = count
        self.error = error
        self.first = self

    async def count(self):
        if self.error:
            raise RuntimeError(self.error)
        return self.match_count

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True


class _FakePage:
    def __init__(self, locator):
        self.locator_result = locator
        self.navigated_to = None
        self.selectors = []

    async def goto(self, url, **_kwargs):
        self.navigated_to = url

    def locator(self, selector):
        self.selectors.append(selector)
        return self.locator_result


class _FakeEngine:
    instances = []
    locator = _FakeLocator()

    def __init__(self, *, browser_type, headless):
        self.browser_type = browser_type
        self.headless = headless
        self.page = _FakePage(self.locator)
        self.stopped = False
        self.instances.append(self)

    async def start(self):
        return None

    async def stop(self):
        self.stopped = True


def _install_fake_engine(monkeypatch, locator=None):
    _FakeEngine.instances = []
    _FakeEngine.locator = locator or _FakeLocator()
    monkeypatch.setattr(
        'apps.ui_automation.playwright_engine.PlaywrightTestEngine',
        _FakeEngine,
    )


def test_format_validation_never_claims_page_validity(monkeypatch):
    element = _element()
    monkeypatch.setattr(locator_validation, '_format_selector', lambda *_args: (True, 'syntax is valid'))

    result = locator_validation.validate_locator(element, {'mode': 'format'})

    assert result['format_valid'] is True
    assert result['validation_status'] == 'UNKNOWN'
    assert result['is_valid'] is False


@pytest.mark.parametrize(('strategy', 'value', 'valid'), [
    ('css', '#submit', True), ('css', '[[[', False),
    ('xpath', '//button', True), ('xpath', '//button[', False),
])
def test_format_checks_use_the_real_syntax_parser(strategy, value, valid):
    result = locator_validation.validate_locator(_element(strategy=strategy, value=value), {'mode': 'format'})
    assert result['format_valid'] is valid
    assert result['validation_status'] == ('UNKNOWN' if valid else 'INVALID')
    assert result['is_valid'] is False


@pytest.mark.parametrize(
    ('count', 'unique', 'expected_status'),
    [(1, True, 'VALID'), (2, True, 'INVALID'), (2, False, 'VALID'), (0, False, 'INVALID')],
)
def test_live_validation_uses_guarded_headless_probe_and_match_policy(
    monkeypatch, count, unique, expected_status,
):
    _install_fake_engine(monkeypatch, _FakeLocator(count))
    monkeypatch.setattr(locator_validation, 'validate_ui_outbound_url', lambda url: url)
    element = _element(unique=unique)
    raw_url = 'https://safe.example.test/path?token=not-returned#fragment'

    result = locator_validation.validate_locator(
        element,
        {'mode': 'live', 'engine': 'playwright', 'browser': 'chrome', 'target_url': raw_url},
    )

    engine, = _FakeEngine.instances
    assert engine.headless is True
    assert engine.browser_type == 'chrome'
    assert engine.page.navigated_to == raw_url
    assert engine.stopped is True
    assert result['validation_status'] == expected_status
    assert result['is_valid'] is (expected_status == 'VALID')
    assert result['match_count'] == count
    assert result['target_url'] == 'https://safe.example.test/'
    assert 'token' not in result['target_url']


def test_live_validation_keeps_policy_denials_unknown_without_starting_browser(monkeypatch):
    monkeypatch.setattr(
        locator_validation,
        'validate_ui_outbound_url',
        lambda _url: (_ for _ in ()).throw(ValueError('blocked')),
    )
    runner = lambda *_args: pytest.fail('browser must not start after policy denial')
    monkeypatch.setattr(locator_validation, '_run_live_probe', runner)

    result = locator_validation.validate_locator(_element(), {'mode': 'live'})

    assert result['validation_status'] == 'UNKNOWN'
    assert result['is_valid'] is False


@pytest.mark.parametrize(
    ('strategy', 'value', 'expected_selector'),
    [
        ('id', '9login:panel.item', r'#\39 login\:panel\.item'),
        ('name', 'user"name\\field', r'[name="user\"name\\field"]'),
    ],
)
def test_live_validation_escapes_id_and_name_as_css_values(
    monkeypatch, strategy, value, expected_selector,
):
    _install_fake_engine(monkeypatch)
    monkeypatch.setattr(locator_validation, 'validate_ui_outbound_url', lambda url: url)

    result = locator_validation.validate_locator(
        _element(strategy=strategy, value=value),
        {'mode': 'live', 'engine': 'playwright', 'browser': 'chrome'},
    )

    engine, = _FakeEngine.instances
    assert result['validation_status'] == 'VALID'
    assert engine.page.selectors == [expected_selector]


@pytest.mark.parametrize(
    ('probe_error', 'expected_status'),
    [('Unexpected token while parsing selector', 'INVALID'), ('Browser context has been closed', 'UNKNOWN')],
)
def test_live_probe_distinguishes_selector_syntax_from_browser_failure(
    monkeypatch, probe_error, expected_status,
):
    _install_fake_engine(monkeypatch, _FakeLocator(error=probe_error))
    monkeypatch.setattr(locator_validation, 'validate_ui_outbound_url', lambda url: url)

    result = locator_validation.validate_locator(_element(), {'mode': 'live'})

    assert result['validation_status'] == expected_status
    assert result['is_valid'] is False


@pytest.mark.django_db
def test_format_endpoint_persists_unknown_instead_of_valid(monkeypatch):
    user = User.objects.create_user(username='locator-format-owner')
    project = UiProject.objects.create(
        name='locator-format',
        base_url='https://locator-format.example.test',
        owner=user,
    )
    strategy = LocatorStrategy.objects.create(name='css')
    element = Element.objects.create(
        project=project,
        name='submit',
        locator_strategy=strategy,
        locator_value='#submit',
        validation_status='VALID',
        created_by=user,
    )
    monkeypatch.setattr(locator_validation, '_format_selector', lambda *_args: (True, 'syntax is valid'))
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post(
        f'/api/ui-automation/elements/{element.pk}/validate_locator/',
        {'mode': 'format'},
        format='json',
    )

    assert response.status_code == 200
    assert response.data['format_valid'] is True
    assert response.data['validation_status'] == 'UNKNOWN'
    assert response.data['is_valid'] is False
    element.refresh_from_db()
    assert element.validation_status == 'UNKNOWN'


@pytest.mark.parametrize('mode', ['format', 'live'])
@pytest.mark.parametrize('changed_field', ['expression', 'strategy'])
def test_unsaved_or_concurrently_changed_locator_is_never_reported_valid(monkeypatch, mode, changed_field):
    element = _element()
    element.locator_strategy_id = 7
    monkeypatch.setattr(locator_validation, '_run_live_probe', lambda *_args: pytest.fail('draft must not start a browser'))
    monkeypatch.setattr(locator_validation, '_format_selector', lambda *_args: pytest.fail('draft must not validate the saved expression'))
    result = locator_validation.validate_locator(element, {
        'mode': mode,
        'expected_locator_value': '#draft' if changed_field == 'expression' else '#submit',
        'expected_locator_strategy_id': 8 if changed_field == 'strategy' else 7,
    })
    assert result['validation_status'] == 'UNKNOWN'
    assert result['is_valid'] is False
    assert '保存' in result['validation_message']


@pytest.mark.django_db
def test_expected_locator_identity_is_checked_by_the_endpoint_without_mutating_the_expression(monkeypatch):
    user = User.objects.create_user(username='locator-draft-owner')
    project = UiProject.objects.create(name='locator-draft', base_url='https://draft.example.test', owner=user)
    strategy = LocatorStrategy.objects.create(name='css')
    element = Element.objects.create(project=project, name='saved', locator_strategy=strategy, locator_value='#saved', created_by=user)
    monkeypatch.setattr(locator_validation, '_run_live_probe', lambda *_args: pytest.fail('stale expression must not probe the saved one'))
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(f'/api/ui-automation/elements/{element.pk}/validate_locator/', {
        'mode': 'live', 'expected_locator_value': '#unsaved', 'expected_locator_strategy_id': strategy.pk,
    }, format='json')
    assert response.status_code == 200
    assert response.data['validation_status'] == 'UNKNOWN'
    assert response.data['is_valid'] is False
    element.refresh_from_db()
    assert element.locator_value == '#saved'
