"""Opt-in device pagination with an authenticated, bounded ID roster."""
from __future__ import annotations

import hashlib
import hmac
import re

from rest_framework import status
from rest_framework.response import Response
from rest_framework.utils.urls import replace_query_param

_MAX_ROSTER_DEVICES = 10_000
_ROSTER_VERSION = re.compile(r'[0-9a-f]{64}\Z')


def _roster_version(user_id, device_ids):
    digest = hashlib.sha256()
    digest.update(f'{user_id}:'.encode('ascii'))
    for device_id in device_ids:
        digest.update(f'{device_id},'.encode('ascii'))
    return digest.hexdigest()


def _changed_response():
    return Response(
        {'detail': '设备列表已变化，请刷新后重试', 'code': 'device_roster_changed'},
        status=status.HTTP_409_CONFLICT,
    )


def list_consistent_devices(view, request):
    """Page IDs, then fetch those IDs: OFFSET must never select a different roster.

    Every request recomputes the visible roster under the usual permissions and
    filters. A later page must match the first request's fingerprint, including
    the authenticated user. Row changes between reading the roster and fetching
    the page are detected by comparing the fetched IDs with that exact page.
    """
    expected = request.query_params.get('expected_roster')
    if expected is None and request.query_params.get('page', '1') != '1':
        return Response({'detail': '后续分页缺少设备列表版本，请从第一页刷新',
                         'code': 'missing_device_roster'}, status=status.HTTP_400_BAD_REQUEST)
    if expected is not None and not _ROSTER_VERSION.fullmatch(expected):
        return Response({'detail': '设备列表版本无效', 'code': 'invalid_device_roster'},
                        status=status.HTTP_400_BAD_REQUEST)

    queryset = view.filter_queryset(view.get_queryset())
    device_ids = list(queryset.order_by('pk').values_list('pk', flat=True)[:_MAX_ROSTER_DEVICES + 1])
    if len(device_ids) > _MAX_ROSTER_DEVICES:
        return Response({'detail': '设备数量超过一次可加载上限，请缩小筛选范围',
                         'code': 'device_roster_limit'}, status=status.HTTP_400_BAD_REQUEST)
    version = _roster_version(request.user.pk, device_ids)
    if expected is not None and not hmac.compare_digest(expected, version):
        return _changed_response()

    page_ids = view.paginate_queryset(device_ids)
    paginated = page_ids is not None
    if page_ids is None:
        page_ids = device_ids
    rows = list(queryset.filter(pk__in=page_ids).order_by('pk'))
    if [row.pk for row in rows] != list(page_ids):
        return _changed_response()

    serialized = view.get_serializer(rows, many=True).data
    if paginated:
        response = view.get_paginated_response(serialized)
        response.data['roster_version'] = version
        for name in ('next', 'previous'):
            if response.data.get(name):
                response.data[name] = replace_query_param(response.data[name], 'expected_roster', version)
        return response
    return Response({'count': len(device_ids), 'next': None, 'previous': None,
                     'results': serialized, 'roster_version': version})
