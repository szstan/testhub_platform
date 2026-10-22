import pytest

from apps.app_automation.device_protocol import (
    DeviceProtocolError,
    capability_fingerprint,
    lease_fence_matches,
    parse_heartbeat,
)


def test_device_heartbeat_is_versioned_and_validated():
    heartbeat = parse_heartbeat({
        'device_id': 'android-1',
        'status': 'online',
        'health_status': 'healthy',
        'capabilities': {'platform': 'android', 'api': 35},
        'agent_id': 'agent-1',
    })

    assert heartbeat.to_message()['protocol_version'] == '1.0'
    assert len(capability_fingerprint(heartbeat.capabilities)) == 64


def test_device_heartbeat_rejects_unknown_status():
    with pytest.raises(DeviceProtocolError):
        parse_heartbeat({'device_id': 'android-1', 'status': 'busy'})


def test_lease_fencing_uses_exact_token_match():
    assert lease_fence_matches('lease-token', 'lease-token') is True
    assert lease_fence_matches('lease-token', 'old-token') is False
    assert lease_fence_matches('', 'lease-token') is False
