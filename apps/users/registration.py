from contextlib import contextmanager
from functools import lru_cache
import hashlib
import re

from django.contrib.auth.hashers import make_password
from django.db import connections, transaction

from .models import User, UserPhoneWriteLock


PHONE_WRITE_LOCK_STRIPES = 256


class DuplicatePhoneError(ValueError):
    pass


@lru_cache(maxsize=32)
def _mysql_phone_collation(alias, database_name):
    """Use the phone column's actual collation, including legacy databases."""
    db = connections[alias]
    field = User._meta.get_field('phone')
    with db.cursor() as cursor:
        cursor.execute(
            'SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS '
            'WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s',
            [User._meta.db_table, field.column],
        )
        row = cursor.fetchone()
    if not row or not all(value and re.fullmatch(r'[A-Za-z0-9_]+', value) for value in row):
        raise RuntimeError('无法确定手机号字段的数据库排序规则')
    return row


def _phone_comparison_key(phone):
    db = connections['default']
    if db.vendor != 'mysql':
        return str(phone).encode('utf-8')
    charset, collation = _mysql_phone_collation(db.alias, db.settings_dict['NAME'])
    with db.cursor() as cursor:
        # Identifiers come from information_schema and are validated above;
        # phone remains a bound parameter. Equal MySQL values have equal weights.
        cursor.execute(
            f'SELECT WEIGHT_STRING(CONVERT(%s USING {charset}) COLLATE {collation})',
            [str(phone)],
        )
        value = cursor.fetchone()[0]
    if value is None:
        raise RuntimeError('无法生成手机号比较键')
    return bytes(value)


def phone_write_lock_id(phone):
    if isinstance(phone, str):
        phone = phone.strip()
    if phone in (None, ''):
        return None

    digest = hashlib.sha256(_phone_comparison_key(phone)).digest()
    return int.from_bytes(digest[:4], 'big') % PHONE_WRITE_LOCK_STRIPES + 1


@contextmanager
def serialized_phone_write(*phones):
    lock_ids = sorted({
        lock_id
        for phone in phones
        if (lock_id := phone_write_lock_id(phone)) is not None
    })
    with transaction.atomic():
        if lock_ids:
            for lock_id in lock_ids:
                UserPhoneWriteLock.objects.get_or_create(pk=lock_id)
            list(
                UserPhoneWriteLock.objects.select_for_update()
                .filter(pk__in=lock_ids)
                .order_by('pk')
            )
        yield


def ensure_phone_available(phone, *, exclude_user_id=None, current_read=False):
    if isinstance(phone, str):
        phone = phone.strip()
    if phone in (None, ''):
        return

    users = User.objects.filter(phone=phone)
    if exclude_user_id is not None:
        users = users.exclude(pk=exclude_user_id)
    if current_read:
        users = users.select_for_update()
    if users.exists():
        raise DuplicatePhoneError('该手机号已被注册')


def _prepare_user_for_creation(user_data):
    user_data = dict(user_data)
    manager = User._default_manager
    username = user_data.pop('username')
    if not username:
        raise ValueError('The given username must be set')
    email = manager.normalize_email(user_data.pop('email', None))
    password = user_data.pop('password', None)

    user = User(
        username=User.normalize_username(username),
        email=email,
        **user_data,
    )
    user.password = make_password(password)
    return manager, user


def create_user_with_unique_phone(**user_data):
    phone = user_data.get('phone')
    if isinstance(phone, str):
        phone = phone.strip()
        user_data['phone'] = phone
    if phone in (None, ''):
        return User.objects.create_user(**user_data)

    manager, user = _prepare_user_for_creation(user_data)
    with serialized_phone_write(phone):
        ensure_phone_available(phone, current_read=True)
        user.save(using=manager._db)
    return user
