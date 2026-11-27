import pytest

from apps.app_automation.models import AppDevice, AppTestExecution
from apps.app_automation.serializers import AppDeviceSerializer


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('device_pending', 'execution_pending', 'expected'),
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
    ],
    ids=['no-pending-action', 'device-marker', 'execution-marker'],
)
def test_device_pending_action_indicator_combines_markers_and_is_read_only(
    device_pending, execution_pending, expected,
):
    device = AppDevice.objects.create(
        device_id=f'serializer-pending-{device_pending}-{execution_pending}',
        device_action_pending=device_pending,
    )
    AppTestExecution.objects.create(
        device=device,
        status='error',
        device_action_pending=execution_pending,
    )

    serializer = AppDeviceSerializer(instance=device)
    assert serializer.data['has_pending_action'] is expected
    assert 'device_action_pending' not in serializer.data
    assert 'lease_token_hash' not in serializer.data
    assert serializer.fields['has_pending_action'].read_only is True
    assert 'device_action_pending' not in serializer.fields

    write_attempt = AppDeviceSerializer(
        instance=device,
        data={
            'device_action_pending': not device_pending,
            'has_pending_action': not expected,
        },
        partial=True,
    )
    assert write_attempt.is_valid(), write_attempt.errors
    write_attempt.save()

    device.refresh_from_db()
    assert device.device_action_pending is device_pending
    assert AppTestExecution.objects.get(device=device).device_action_pending is execution_pending
    assert AppDeviceSerializer(instance=device).data['has_pending_action'] is expected


@pytest.mark.django_db
def test_queryset_device_list_uses_one_exists_query_for_execution_markers(django_assert_num_queries):
    devices = [
        AppDevice.objects.create(device_id=f'annotated-pending-{index}')
        for index in range(3)
    ]
    AppTestExecution.objects.create(
        device=devices[1],
        status='error',
        device_action_pending=True,
    )
    queryset = AppDevice.objects.filter(pk__in=[device.pk for device in devices])

    with django_assert_num_queries(1):
        data = AppDeviceSerializer(queryset, many=True).data

    assert {item['id']: item['has_pending_action'] for item in data} == {
        devices[0].pk: False,
        devices[1].pk: True,
        devices[2].pk: False,
    }


@pytest.mark.django_db
def test_list_of_devices_batches_execution_marker_lookup(django_assert_num_queries):
    devices = [
        AppDevice.objects.create(device_id=f'plain-list-pending-{index}')
        for index in range(3)
    ]
    AppTestExecution.objects.create(
        device=devices[2],
        status='error',
        device_action_pending=True,
    )

    with django_assert_num_queries(1):
        data = AppDeviceSerializer(devices, many=True).data

    assert [item['has_pending_action'] for item in data] == [False, False, True]
