"""Prove analytics is migratable; pytest's normal schema sync is not sufficient."""
import os
from pathlib import Path
import subprocess
import sys


def test_analytics_initial_migration_creates_event_table_on_fresh_database(tmp_path):
    script = r'''
import os
import sys
os.environ['DJANGO_SETTINGS_MODULE'] = 'backend.test_settings'
os.environ['ANALYTICS_ENABLED'] = 'true'
sys.argv = ['manage.py', 'migrate']
from django.conf import settings
settings.MIGRATION_MODULES = {}
settings.DATABASES['default']['NAME'] = os.environ['ANALYTICS_MIGRATION_TEST_DB']
import django
django.setup()
from django.core.management import call_command
call_command('migrate', 'analytics', verbosity=0, interactive=False)
from apps.analytics.models import AnalyticsEvent, RegistrationStats
from apps.users.models import User
from django.db import connection
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState
from django.apps import apps
user = User.objects.create_user(username='migrated-analytics-owner')
event = AnalyticsEvent.objects.create(event_name='fresh-migration', user=user)
assert event.pk and AnalyticsEvent.objects.get(pk=event.pk).user_id == user.pk
assert 'analytics_events' in connection.introspection.table_names()
assert RegistrationStats._meta.proxy
loader = MigrationLoader(connection)
changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(graph=loader.graph, trim_to_apps={'analytics'})
assert not changes.get('analytics'), changes.get('analytics')
'''
    child_env = dict(os.environ)
    child_env['ANALYTICS_MIGRATION_TEST_DB'] = str(tmp_path / 'analytics-migration.sqlite3')
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[2],
        env=child_env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
