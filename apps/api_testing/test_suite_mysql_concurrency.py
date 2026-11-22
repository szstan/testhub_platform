from threading import Event, Thread
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from rest_framework.test import APIClient

from apps.api_testing.models import ApiCollection, ApiProject, ApiRequest
from apps.api_testing.models import TestSuite as ApiTestSuite, TestSuiteRequest as ApiTestSuiteRequest
from apps.api_testing.serializers import TestSuiteSerializer as ApiTestSuiteSerializer
from apps.api_testing.views import TestSuiteViewSet as ApiTestSuiteViewSet


@pytest.mark.django_db(transaction=True)
def test_mysql_suite_move_blocks_concurrent_add_from_the_old_project():
    if connection.vendor != 'mysql':
        pytest.skip('SQLite does not implement select_for_update row locks')

    User = get_user_model()
    owner = User.objects.create_user(username='mysql-suite-owner')
    target_owner = User.objects.create_user(username='mysql-suite-target-owner')
    source = ApiProject.objects.create(
        name='mysql suite source', project_type='HTTP', status='IN_PROGRESS', owner=owner,
    )
    target = ApiProject.objects.create(
        name='mysql suite target', project_type='HTTP', status='IN_PROGRESS', owner=target_owner,
    )
    target.members.add(owner)
    collection = ApiCollection.objects.create(name='source collection', project=source)
    api_request = ApiRequest.objects.create(
        collection=collection, name='source request', method='GET',
        url='https://example.invalid', created_by=owner,
    )
    suite = ApiTestSuite.objects.create(project=source, name='suite', created_by=owner)

    move_has_lock = Event()
    add_looked_up_suite = Event()
    add_finished = Event()
    release_move = Event()
    results = {}
    errors = []
    original_update = ApiTestSuiteSerializer.update
    original_get_object = ApiTestSuiteViewSet.get_object

    def pause_move(serializer, instance, validated_data):
        if instance.pk == suite.pk and validated_data.get('project') == target:
            move_has_lock.set()
            if not release_move.wait(timeout=10):
                raise TimeoutError('suite move was not released')
        return original_update(serializer, instance, validated_data)

    def observe_add_lookup(view):
        instance = original_get_object(view)
        if view.action == 'add_requests' and instance.pk == suite.pk:
            add_looked_up_suite.set()
        return instance

    def move_suite():
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(user=owner)
            results['move'] = client.patch(
                f'/api/api-testing/test-suites/{suite.pk}/', {'project': target.pk}, format='json',
            ).status_code
        except Exception as exc:
            errors.append(exc)
        finally:
            close_old_connections()

    def add_old_request():
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(user=owner)
            results['add'] = client.post(
                f'/api/api-testing/test-suites/{suite.pk}/add-requests/',
                {'request_ids': [api_request.pk]}, format='json',
            ).status_code
        except Exception as exc:
            errors.append(exc)
        finally:
            add_finished.set()
            close_old_connections()

    move_thread = Thread(target=move_suite)
    add_thread = Thread(target=add_old_request)
    with (
        patch.object(ApiTestSuiteSerializer, 'update', new=pause_move),
        patch.object(ApiTestSuiteViewSet, 'get_object', new=observe_add_lookup),
    ):
        move_thread.start()
        try:
            assert move_has_lock.wait(timeout=8)
            add_thread.start()
            assert add_looked_up_suite.wait(timeout=8)
            assert not add_finished.wait(timeout=0.25)
        finally:
            release_move.set()
            move_thread.join(timeout=15)
            if add_thread.ident is not None:
                add_thread.join(timeout=15)

    assert not move_thread.is_alive()
    assert not add_thread.is_alive()
    assert not errors
    assert results == {'move': 200, 'add': 400}
    suite.refresh_from_db()
    assert suite.project_id == target.pk
    assert not ApiTestSuiteRequest.objects.filter(test_suite=suite).exists()
