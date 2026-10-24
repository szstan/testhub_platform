#!/usr/bin/env python3
"""Create an initial TestHub administrator through an interactive safe flow."""

import getpass
import os
import sys
from pathlib import Path

BASE_DIR = Path(os.environ.get('TESTHUB_PROJECT_DIR', '/opt/testhub_platform')).resolve()
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')

import django

django.setup()

from apps.users.models import User  # noqa: E402


def _required_secret(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    if not sys.stdin.isatty():
        raise RuntimeError(f'{name} must be provided in a non-interactive environment')
    return getpass.getpass(f'{name}: ')


def create_admin_user():
    username = os.environ.get('TESTHUB_ADMIN_USERNAME', 'admin').strip()
    email = os.environ.get('TESTHUB_ADMIN_EMAIL', '').strip()
    password = _required_secret('TESTHUB_ADMIN_PASSWORD')
    if len(password) < 12:
        raise ValueError('TESTHUB_ADMIN_PASSWORD must contain at least 12 characters')
    if not username:
        raise ValueError('TESTHUB_ADMIN_USERNAME must not be empty')

    user, created = User.objects.get_or_create(
        username=username,
        defaults={
            'email': email,
            'is_staff': True,
            'is_superuser': True,
        },
    )
    if created:
        user.set_password(password)
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=['password', 'is_staff', 'is_superuser'])
        print(f'Administrator {username!r} created.')
    else:
        print(f'Administrator {username!r} already exists; no password change was made.')
    return user


if __name__ == '__main__':
    try:
        create_admin_user()
    except Exception as exc:
        print(f'Administrator initialization failed: {exc}', file=sys.stderr)
        sys.exit(1)
