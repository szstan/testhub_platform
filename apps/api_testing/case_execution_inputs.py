"""Lock, verify, and detach the inputs for a queued API case execution."""

from __future__ import annotations

import hmac
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace

from .case_execution_snapshot import (
    api_case_execution_fingerprint,
    api_case_execution_payload_hash,
)
from .models import (
    ApiCollection,
    ApiProject,
    ApiRequest,
    ApiTestCase,
    ApiTestCaseStep,
    Environment,
)


class ExecutionSnapshotError(ValueError):
    """The persisted execution no longer matches its submitted source inputs."""


@dataclass(frozen=True)
class FrozenApiCaseInputs:
    test_case: object
    environment: object
    steps: tuple


class _FrozenMemberQuery:
    def __init__(self, member_ids, requested_id):
        self._member_ids = member_ids
        self._requested_id = requested_id

    def exists(self):
        return self._requested_id in self._member_ids


class _FrozenMembers:
    def __init__(self, member_ids):
        self._member_ids = frozenset(member_ids)

    def filter(self, *, pk=None):
        return _FrozenMemberQuery(self._member_ids, pk)


def _frozen_project(project, member_ids):
    return SimpleNamespace(
        pk=project.pk,
        id=project.pk,
        owner_id=project.owner_id,
        members=_FrozenMembers(member_ids),
    )


def freeze_api_case_execution(execution, variable_overrides, *, required=False):
    """Verify the submitted digest and return model-shaped values with no ORM links."""
    if not execution.idempotency_payload_hash:
        if required or execution.idempotency_key:
            raise ExecutionSnapshotError('执行配置快照缺失，已阻止执行')
        return None
    if (
        not isinstance(variable_overrides, dict)
        or len(execution.idempotency_payload_hash) != 64
        or any(character not in '0123456789abcdef' for character in execution.idempotency_payload_hash)
    ):
        raise ExecutionSnapshotError('执行配置快照无效，已阻止执行')

    project = ApiProject.objects.select_for_update().filter(pk=execution.project_id).first()
    case = ApiTestCase.objects.select_for_update().filter(pk=execution.test_case_id).first()
    if project is None or case is None or case.project_id != project.pk:
        raise ExecutionSnapshotError('执行用例或项目已变化，已阻止执行')

    member_ids = list(project.members.values_list('pk', flat=True))
    frozen_project = _frozen_project(project, member_ids)

    environment = None
    if execution.environment_id is not None:
        environment = Environment.objects.select_for_update().filter(
            pk=execution.environment_id,
        ).first()
        if environment is None:
            raise ExecutionSnapshotError('执行环境已变化，已阻止执行')

    steps = list(ApiTestCaseStep.objects.select_for_update().filter(
        test_case=case,
    ).order_by('order', 'pk'))
    request_ids = sorted({step.request_id for step in steps})
    request_locations = dict(ApiRequest.objects.filter(
        pk__in=request_ids,
    ).values_list('pk', 'collection_id'))
    if len(request_locations) != len(request_ids):
        raise ExecutionSnapshotError('步骤请求已变化，已阻止执行')

    collection_ids = sorted({collection_id for collection_id in request_locations.values() if collection_id})
    collections = {
        collection.pk: collection
        for collection in ApiCollection.objects.select_for_update().filter(
            pk__in=collection_ids,
        ).order_by('pk')
    }
    requests = {
        api_request.pk: api_request
        for api_request in ApiRequest.objects.select_for_update().filter(
            pk__in=request_ids,
        ).order_by('pk')
    }
    if len(collections) != len(collection_ids) or len(requests) != len(request_ids):
        raise ExecutionSnapshotError('步骤请求集合已变化，已阻止执行')

    for step in steps:
        api_request = requests[step.request_id]
        if api_request.collection_id != request_locations[api_request.pk]:
            raise ExecutionSnapshotError('步骤请求所属集合已变化，已阻止执行')
        collection = collections.get(api_request.collection_id)
        api_request.collection = collection
        step.request = api_request

    fingerprint = api_case_execution_fingerprint(case, environment=environment, steps=steps)
    payload_hash = api_case_execution_payload_hash(
        fingerprint,
        test_case_id=execution.test_case_id,
        environment_id=execution.environment_id,
        variables=variable_overrides,
    )
    if not hmac.compare_digest(payload_hash, execution.idempotency_payload_hash):
        raise ExecutionSnapshotError('用例执行配置已变化，已阻止执行')

    frozen_case = SimpleNamespace(**deepcopy({
        field.attname: getattr(case, field.attname)
        for field in case._meta.concrete_fields
        if not field.primary_key
    }))
    frozen_case.pk = frozen_case.id = case.pk
    frozen_case.project = frozen_project

    frozen_environment = None
    if environment is not None:
        frozen_environment = SimpleNamespace(**deepcopy({
            field.attname: getattr(environment, field.attname)
            for field in environment._meta.concrete_fields
            if not field.primary_key
        }))
        frozen_environment.pk = frozen_environment.id = environment.pk
        frozen_environment.project = frozen_project
        if hasattr(environment, 'default_headers'):
            frozen_environment.default_headers = deepcopy(environment.default_headers)

    frozen_steps = []
    for step in steps:
        frozen_step = SimpleNamespace(**deepcopy({
            field.attname: getattr(step, field.attname)
            for field in step._meta.concrete_fields
            if not field.primary_key
        }))
        frozen_step.pk = frozen_step.id = step.pk

        source_request = step.request
        frozen_request = SimpleNamespace(**deepcopy({
            field.attname: getattr(source_request, field.attname)
            for field in source_request._meta.concrete_fields
            if not field.primary_key
        }))
        frozen_request.pk = frozen_request.id = source_request.pk

        source_collection = source_request.collection
        if source_collection is None:
            frozen_request.collection = None
        else:
            frozen_collection = SimpleNamespace(
                pk=source_collection.pk,
                id=source_collection.pk,
                project_id=source_collection.project_id,
                project=frozen_project,
            )
            frozen_request.collection = frozen_collection
        frozen_step.request = frozen_request
        frozen_steps.append(frozen_step)

    return FrozenApiCaseInputs(
        test_case=frozen_case,
        environment=frozen_environment,
        steps=tuple(frozen_steps),
    )
