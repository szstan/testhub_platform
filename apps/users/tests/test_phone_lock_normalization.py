from threading import Barrier, Thread

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from apps.users.registration import (
    DuplicatePhoneError,
    create_user_with_unique_phone,
    phone_write_lock_id,
)


@pytest.mark.django_db(transaction=True)
def test_mysql_collation_equal_phone_numbers_share_a_lock_and_cannot_race():
    if connection.vendor != 'mysql':
        pytest.skip('MySQL collation weights are not used by SQLite')

    first = '13800138000'
    equivalent = '13８００１３８０００'
    User = get_user_model()
    probe = User.objects.create_user(username='phone-collation-probe', phone=first)
    if not User.objects.filter(phone=equivalent).exists():
        pytest.skip('The configured phone collation distinguishes these values')
    probe.delete()
    assert phone_write_lock_id(first) == phone_write_lock_id(equivalent)

    barrier = Barrier(2, timeout=10)
    results = []
    errors = []

    def register(username, phone):
        close_old_connections()
        try:
            barrier.wait()
            create_user_with_unique_phone(username=username, password='StrongPass123', phone=phone)
            results.append('created')
        except DuplicatePhoneError:
            results.append('duplicate')
        except Exception as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    threads = [
        Thread(target=register, args=('phone-collation-first', first)),
        Thread(target=register, args=('phone-collation-second', equivalent)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert sorted(results) == ['created', 'duplicate']
    assert User.objects.filter(phone=first).count() == 1
