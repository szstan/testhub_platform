from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.app_automation.models import AppComponent
from apps.core.management.commands import load_component_pack


@pytest.mark.django_db
def test_overwrite_updates_pack_components_without_deleting_unrelated_components(tmp_path, monkeypatch):
    built_in = AppComponent.objects.create(
        name='Old built-in', type='declared', category='old', default_config={'old': True},
    )
    unrelated = AppComponent.objects.create(
        name='User component', type='user_component', category='custom',
        default_config={'keep': True},
    )
    monkeypatch.setattr(load_component_pack, '__file__', str(tmp_path / 'load_component_pack.py'))
    Path(tmp_path, 'ui-component-pack.yaml').write_text(
        'name: fixture-pack\n'
        'components:\n'
        '  - type: declared\n'
        '    name: Updated built-in\n'
        '    category: action\n'
        '    default_config:\n'
        '      enabled_by_pack: true\n',
        encoding='utf-8',
    )

    output = StringIO()
    call_command('load_component_pack', '--overwrite', stdout=output)

    built_in.refresh_from_db()
    unrelated.refresh_from_db()
    assert built_in.name == 'Updated built-in'
    assert built_in.default_config == {'enabled_by_pack': True}
    assert unrelated.name == 'User component'


@pytest.mark.django_db
def test_malformed_or_duplicate_pack_is_rejected_before_any_update(tmp_path, monkeypatch):
    component = AppComponent.objects.create(
        name='Existing component', type='declared', category='old',
        default_config={'keep': True},
    )
    monkeypatch.setattr(load_component_pack, '__file__', str(tmp_path / 'load_component_pack.py'))
    Path(tmp_path, 'ui-component-pack.yaml').write_text(
        'name: invalid-pack\n'
        'components:\n'
        '  - type: declared\n'
        '    name: First update\n'
        '  - type: declared\n'
        '    name: Duplicate update\n',
        encoding='utf-8',
    )

    with pytest.raises(CommandError, match='duplicate component type'):
        call_command('load_component_pack', '--overwrite', stdout=StringIO())

    component.refresh_from_db()
    assert component.name == 'Existing component'
    assert component.default_config == {'keep': True}
    assert AppComponent.objects.count() == 1
