"""Test settings shared by the core and upstream feature suites."""
from backend.settings import *  # noqa: F401,F403

SECRET_KEY = 'test-secret-key'
DEBUG = True
ROOT_URLCONF = 'backend.urls'

class DisableMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = DisableMigrations()

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': '/tmp/testhub_sync.sqlite3',
    }
}

PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

AUTH_PASSWORD_VALIDATORS = []

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    },
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

REDIS_URL = 'redis://localhost:6379/15'

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'test-cache',
    }
}

MEDIA_ROOT = Path('/tmp/testhub_sync_media')
