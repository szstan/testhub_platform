import socket

import pytest
from django.test import override_settings
from rest_framework import serializers

from apps.ui_automation.serializers import UiProjectCreateSerializer, UiProjectUpdateSerializer


@pytest.mark.parametrize('serializer_type', [UiProjectCreateSerializer, UiProjectUpdateSerializer])
@pytest.mark.parametrize('address', ['fd00:ec2::254', 'fd20:ce::254', 'fd00:ec2::23'])
def test_ui_projects_reject_ipv6_cloud_metadata_even_in_an_allowed_ula(serializer_type, address):
    with override_settings(UI_ALLOW_PRIVATE_TARGETS=True, UI_ALLOWED_PRIVATE_CIDRS=['fc00::/7']):
        with pytest.raises(serializers.ValidationError, match='受限网络'):
            serializer_type().validate_base_url(f'http://[{address}]/metadata/')


@pytest.mark.parametrize('serializer_type', [UiProjectCreateSerializer, UiProjectUpdateSerializer])
@pytest.mark.parametrize('address', ['fd20:ce::254', 'fd00:ec2::23'])
def test_ui_projects_reject_dns_names_resolving_to_ipv6_cloud_service(monkeypatch, serializer_type, address):
    monkeypatch.setattr(
        'apps.core.network.socket.getaddrinfo',
        lambda _host, port, **_kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port, 0, 0)),
        ],
    )
    with override_settings(UI_ALLOW_PRIVATE_TARGETS=True, UI_ALLOWED_PRIVATE_CIDRS=['fc00::/7']):
        with pytest.raises(serializers.ValidationError, match='受限网络'):
            serializer_type().validate_base_url('http://metadata.example.test/metadata/')
