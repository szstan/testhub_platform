import pytest

from apps.core.network import UnsafeOutboundURL, validate_outbound_url


def test_validate_outbound_url_rejects_private_targets_by_default():
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url('http://127.0.0.1:8000/health')


def test_validate_outbound_url_rejects_credentials_and_invalid_ports():
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url('https://user:password@example.com/api')
    with pytest.raises(UnsafeOutboundURL):
        validate_outbound_url('https://example.com:99999/api')


def test_validate_outbound_url_allows_private_targets_only_when_explicit():
    assert validate_outbound_url('http://127.0.0.1:8000/health', allow_private=True) == 'http://127.0.0.1:8000/health'
