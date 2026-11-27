"""Parent-owned Appium execution sessions with child-process cleanup fencing."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from .access import package_action_allowed, project_accessible
from .appium_models import AppiumSession
from .appium_runtime import (
    AppiumClientUnavailable,
    AppiumConfigurationError,
    AppiumInputError,
    AppiumOutcomeUnknown,
    AppiumRuntimeError,
    _client_config,
    _load_client,
    _new_session_timeout,
    _options,
    attach_driver,
    build_capabilities,
    create_driver,
    endpoint_for_device,
    quit_driver,
    system_port_for_device,
)
from .constants import ExecutionEngine
from .device_pool import DeviceLeaseError, assert_device_execution_lease
from .models import AppDevice, AppTestExecution


class AppiumExecutionError(RuntimeError):
    """A safe, non-sensitive execution-session coordination error."""


@dataclass(frozen=True)
class ExecutionSessionHandle:
    id: str
    execution_id: int
    lease_generation: int
    parent_pid: int
    parent_start_token: str


def _process_start_token(pid):
    try:
        import psutil
        return f"{psutil.Process(pid).create_time():.6f}"
    except Exception:
        raise AppiumExecutionError("The execution parent process cannot be identified") from None


def _assert_parent_alive(session):
    if not session.parent_pid or not session.parent_start_token:
        raise AppiumExecutionError("The execution session has no parent-process proof")
    if _process_start_token(session.parent_pid) != session.parent_start_token:
        raise AppiumExecutionError("The execution parent process is no longer active")


def _load_execution_snapshot(execution_id, *, require_action_pending):
    execution = AppTestExecution.objects.select_related(
        "device", "user", "test_case__project", "test_case__app_package",
    ).filter(pk=execution_id).first()
    if (
        execution is None
        or execution.engine != ExecutionEngine.APPIUM
        or execution.status != "running"
        or execution.stop_requested
        or execution.device is None
        or execution.user is None
        or execution.test_case is None
        or execution.test_case.project is None
        or execution.test_case.app_package is None
        or execution.device_lease_generation is None
        or (require_action_pending and not execution.device_action_pending)
    ):
        raise AppiumExecutionError("The Appium execution lease is unavailable")
    if not project_accessible(execution.user, execution.test_case.project):
        raise AppiumExecutionError("The Appium execution project is unavailable")
    if not package_action_allowed(execution.user, execution.test_case.app_package, execution.test_case.project):
        raise AppiumExecutionError("The Appium execution package is unavailable")
    return execution


def _validate_execution_inputs(execution, device_id, package_name):
    if (
        str(execution.device.device_id) != str(device_id)
        or execution.test_case.app_package.package_name != package_name
        or not execution.test_case.project.appium_enabled
    ):
        raise AppiumExecutionError("The Appium execution configuration changed")
    assert_device_execution_lease(
        execution.device_id,
        execution.user,
        execution.device_lease_generation,
        execution_id=execution.pk,
    )
    try:
        endpoint = endpoint_for_device(execution.device)
        system_port = system_port_for_device(execution.device)
        capabilities = build_capabilities(execution.device, execution.test_case.app_package, system_port, auto_launch=False)
        bindings = _load_client()
        _options(bindings, capabilities)
        _client_config(bindings, endpoint, _new_session_timeout())
    except AppiumRuntimeError as exc:
        raise AppiumExecutionError(getattr(exc, "code", "appium_configuration_invalid")) from None
    except AppiumClientUnavailable as exc:
        raise AppiumExecutionError(getattr(exc, "code", "appium_client_unavailable")) from None
    return endpoint, system_port, capabilities


def preflight_execution_session(execution_id, device_id, package_name):
    """Validate server configuration and execution bindings without device I/O."""
    execution = _load_execution_snapshot(execution_id, require_action_pending=False)
    endpoint, system_port, _capabilities = _validate_execution_inputs(
        execution, device_id, package_name,
    )
    if (
        execution.device.device_action_pending
        or AppiumSession.objects.filter(
            device=execution.device,
            status__in=("starting", "active", "closing", "unknown"),
        ).exists()
    ):
        raise AppiumExecutionError("The device is busy or requires Appium recovery")
    return {"endpoint": endpoint, "system_port": system_port}


def _execution_session_fence(execution_id, session_id, *, closing=False):
    session = AppiumSession.objects.select_related(
        "device", "user", "project", "package", "execution",
    ).filter(
        pk=session_id,
        kind="execution",
        execution_id=execution_id,
    ).first()
    if session is None:
        raise AppiumExecutionError("The Appium execution session is unavailable")
    execution = AppTestExecution.objects.select_related("device", "user", "test_case").filter(
        pk=execution_id,
    ).first()
    if (
        execution is None
        or execution.status != "running"
        or (execution.stop_requested and not closing)
        or execution.engine != ExecutionEngine.APPIUM
        or execution.device_id != session.device_id
        or execution.user_id != session.user_id
        or execution.device_lease_generation != session.execution_generation
        or session.lease_generation != session.execution_generation
        or session.device_action_epoch != session.device.device_action_epoch
        or not session.device.device_action_pending
        or execution.device_action_pending is not True
        or session.project_id != getattr(execution.test_case, "project_id", None)
        or session.package_id != getattr(execution.test_case, "app_package_id", None)
    ):
        raise AppiumExecutionError("The Appium execution session fence changed")
    assert_device_execution_lease(
        session.device_id,
        session.user,
        session.execution_generation,
        execution_id=execution.pk,
    )
    _assert_parent_alive(session)
    return session, execution


def _clear_known_pre_io_failure(execution_id):
    """Reconcile a rejected reservation without clearing a session's active marker."""
    device_id = AppTestExecution.objects.filter(pk=execution_id).values_list('device_id', flat=True).first()
    if device_id is None:
        return False
    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(pk=device_id).first()
        execution = AppTestExecution.objects.select_for_update().filter(pk=execution_id, device_id=device_id).first()
        if device is None or execution is None or execution.status != 'running' or not execution.device_action_pending:
            return False
        if device.device_action_pending or AppiumSession.objects.filter(
            device_id=device_id, status__in=('starting', 'active', 'closing', 'unknown'),
        ).exists():
            return False
        try:
            assert_device_execution_lease(
                device_id, execution.user, execution.device_lease_generation, execution_id=execution.pk,
            )
        except DeviceLeaseError:
            return False
        execution.device_action_pending = False
        execution.save(update_fields=['device_action_pending', 'updated_at'])
        return True


def _proven_batch_pending_sibling_ids(execution, dispatch_id, batch_execution_ids):
    """Return pending siblings only when the persisted dispatch proves ownership."""
    if not isinstance(dispatch_id, str) or not dispatch_id or batch_execution_ids is None:
        return set()
    try:
        batch_ids = list(batch_execution_ids)
    except TypeError:
        return set()
    if (
        not batch_ids
        or any(type(execution_id) is not int for execution_id in batch_ids)
        or len(set(batch_ids)) != len(batch_ids)
        or execution.pk not in batch_ids
    ):
        return set()

    rows = list(
        AppTestExecution.objects.select_for_update().filter(task_id=dispatch_id).order_by("pk")
    )
    if {row.pk for row in rows} != set(batch_ids):
        return set()
    binding = (execution.device_id, execution.user_id, execution.device_lease_generation)
    if any(
        row.task_id != dispatch_id
        or (row.device_id, row.user_id, row.device_lease_generation) != binding
        for row in rows
    ):
        return set()
    if (
        execution.status != "running"
        or execution.started_at is None
        or execution.finished_at is not None
        or execution.stop_requested
        or not execution.device_action_pending
    ):
        return set()

    pending_ids = set()
    for row in rows:
        if row.pk == execution.pk:
            continue
        if row.status == "running":
            return set()
        if row.status == "pending":
            if (
                row.started_at is not None
                or row.finished_at is not None
                or row.stop_requested
                or row.device_action_pending
            ):
                return set()
            pending_ids.add(row.pk)
    return pending_ids


def _reserve_execution_session(
    execution_id,
    device_id,
    package_name,
    *,
    parent_pid,
    batch_dispatch_id,
    batch_execution_ids,
):
    execution = _load_execution_snapshot(execution_id, require_action_pending=True)
    endpoint, system_port, capabilities = _validate_execution_inputs(
        execution, device_id, package_name,
    )
    if type(parent_pid) is not int or parent_pid != os.getpid():
        raise AppiumExecutionError("Only the owning worker can start an execution session")
    parent_start_token = _process_start_token(parent_pid)
    device = execution.device
    parent_user_id = execution.user_id

    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().get(pk=device.pk)
        execution = AppTestExecution.objects.select_for_update().select_related(
            "user", "test_case__project", "test_case__app_package",
        ).get(pk=execution_id)
        assert_device_execution_lease(
            device.pk, execution.user, execution.device_lease_generation,
            execution_id=execution.pk,
        )
        allowed_siblings = _proven_batch_pending_sibling_ids(
            execution, batch_dispatch_id, batch_execution_ids,
        )
        competing_ids = set(
            AppTestExecution.objects.select_for_update().filter(
                device=device, status__in=("pending", "running"),
            ).exclude(pk=execution.pk).values_list("pk", flat=True)
        )
        if (
            execution.engine != ExecutionEngine.APPIUM
            or execution.status != "running"
            or execution.stop_requested
            or not execution.device_action_pending
            or device.device_action_pending
            or device.lease_generation != execution.device_lease_generation
            or device.locked_by_id != execution.user_id
            or execution.user_id != parent_user_id
            or competing_ids - allowed_siblings
            or AppiumSession.objects.filter(
                device=device,
                status__in=("starting", "active", "closing", "unknown"),
            ).exists()
        ):
            raise AppiumExecutionError("The Appium execution session cannot be reserved")
        device.device_action_pending = True
        device.device_action_epoch += 1
        device.save(update_fields=["device_action_pending", "device_action_epoch", "updated_at"])
        session = AppiumSession.objects.create(
            kind="execution",
            device=device,
            user=execution.user,
            project=execution.test_case.project,
            package=execution.test_case.app_package,
            execution=execution,
            lease_generation=execution.device_lease_generation,
            execution_generation=execution.device_lease_generation,
            device_action_epoch=device.device_action_epoch,
            system_port=system_port,
            appium_endpoint=endpoint,
            capabilities=capabilities,
            status="starting",
            request_key=str(uuid.uuid4()),
            parent_pid=parent_pid,
            parent_start_token=parent_start_token,
        )
    return execution, device, parent_user_id, endpoint, system_port, capabilities, session


def start_execution_session(
    execution_id,
    device_id,
    package_name,
    *,
    parent_pid=None,
    batch_dispatch_id=None,
    batch_execution_ids=None,
):
    """Reserve, create, and persist a session before launching the pytest child."""
    parent_pid = os.getpid() if parent_pid is None else parent_pid
    try:
        execution, device, parent_user_id, endpoint, system_port, capabilities, session = (
            _reserve_execution_session(
                execution_id,
                device_id,
                package_name,
                parent_pid=parent_pid,
                batch_dispatch_id=batch_dispatch_id,
                batch_execution_ids=batch_execution_ids,
            )
        )
    except AppiumExecutionError:
        _clear_known_pre_io_failure(execution_id)
        raise

    try:
        if endpoint_for_device(device) != endpoint:
            raise AppiumConfigurationError("The configured Appium endpoint changed")
        driver = create_driver(device, execution.test_case.app_package, system_port=system_port, auto_launch=False)
    except (AppiumClientUnavailable, AppiumConfigurationError, AppiumInputError):
        _settle_known_start_failure(session.pk)
        raise AppiumExecutionError("Appium could not start on this worker") from None
    except AppiumOutcomeUnknown:
        _mark_session_unknown(session.pk, "appium_start_outcome_unknown")
        raise AppiumExecutionError("Appium session creation could not be confirmed") from None
    except Exception:
        _mark_session_unknown(session.pk, "appium_start_outcome_unknown")
        raise AppiumExecutionError("Appium session creation could not be confirmed") from None

    try:
        with transaction.atomic(durable=True):
            current_device = AppDevice.objects.select_for_update().get(pk=device.pk)
            current = AppiumSession.objects.select_for_update().get(pk=session.pk)
            current_execution = AppTestExecution.objects.select_for_update().get(pk=execution.pk)
            if (
                current_device.locked_by_id != parent_user_id
                or current_device.lease_generation != current.lease_generation
                or current_device.device_action_epoch != current.device_action_epoch
                or not current_device.device_action_pending
                or current_execution.status != "running"
                or current_execution.stop_requested
                or current_execution.device_lease_generation != current.execution_generation
                or not current_execution.device_action_pending
                or current.status != "starting"
            ):
                raise AppiumExecutionError("The Appium execution lease changed during startup")
            current.remote_session_id = driver.session_id
            current.capabilities = dict(capabilities)
            current.status = "active"
            current.save(update_fields=["remote_session_id", "capabilities", "status", "updated_at"])
    except Exception:
        try:
            quit_driver(driver)
        except AppiumOutcomeUnknown:
            _mark_session_unknown(session.pk, "appium_start_persistence_unknown")
            raise AppiumExecutionError("Appium session cleanup could not be confirmed") from None
        _settle_known_start_failure(session.pk)
        raise AppiumExecutionError("Appium session ownership could not be persisted") from None

    return ExecutionSessionHandle(
        id=str(session.pk),
        execution_id=execution.pk,
        lease_generation=execution.device_lease_generation,
        parent_pid=parent_pid,
        parent_start_token=session.parent_start_token,
    )


def _settle_known_start_failure(session_id):
    session = AppiumSession.objects.filter(pk=session_id).values(
        "device_id", "user_id", "lease_generation", "device_action_epoch", "execution_id",
    ).first()
    if session is None:
        return False
    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(pk=session["device_id"]).first()
        current = AppiumSession.objects.select_for_update().filter(pk=session_id).first()
        if (
            device is None
            or current is None
            or device.locked_by_id != session["user_id"]
            or device.lease_generation != session["lease_generation"]
            or device.device_action_epoch != session["device_action_epoch"]
            or not device.device_action_pending
        ):
            return False
        current.status = "failed"
        current.failure_code = "appium_start_failed"
        current.closed_at = timezone.now()
        current.save(update_fields=["status", "failure_code", "closed_at", "updated_at"])
        device.device_action_pending = False
        device.save(update_fields=["device_action_pending", "updated_at"])
    if session["execution_id"]:
        from .execution_admission import clear_device_action_pending
        try:
            clear_device_action_pending(session["execution_id"])
        except DeviceLeaseError:
            return False
    return True


def _mark_session_unknown(session_id, error_code):
    AppiumSession.objects.filter(
        pk=session_id,
        status__in=("starting", "active", "closing"),
    ).update(status="unknown", failure_code=error_code, updated_at=timezone.now())


def _load_execution_handle(execution_id, session_id):
    session, execution = _execution_session_fence(execution_id, session_id)
    if session.status != "active" or not session.remote_session_id:
        raise AppiumExecutionError("The Appium execution session is not active")
    return session, execution


def attach_execution_session(execution_id, session_id):
    """Attach the pytest child to its parent-created session without creating another."""
    try:
        session, _execution = _load_execution_handle(execution_id, session_id)
        if os.getppid() != session.parent_pid:
            raise AppiumExecutionError("The Appium execution parent changed")
        return attach_driver(session)
    except AppiumRuntimeError:
        raise
    except Exception:
        raise AppiumExecutionError("The Appium execution session is unavailable") from None


def guard_execution_session(execution_id, session_id):
    """Revalidate execution, lease, parent process, and action epoch before each device call."""
    try:
        _load_execution_handle(execution_id, session_id)
    except Exception:
        raise DeviceLeaseError("The Appium execution lease or parent process was lost") from None
    return True


def finish_execution_session(owned_session, *, child_exit_confirmed):
    """Close only after the owning parent proves the pytest child was reaped."""
    if not isinstance(owned_session, ExecutionSessionHandle):
        return False
    session = AppiumSession.objects.filter(
        pk=owned_session.id,
        execution_id=owned_session.execution_id,
        kind="execution",
        execution_generation=owned_session.lease_generation,
    ).first()
    if session is None:
        return False
    if (
        type(child_exit_confirmed) is not bool
        or not child_exit_confirmed
        or owned_session.parent_pid != os.getpid()
        or session.parent_pid != owned_session.parent_pid
        or session.parent_start_token != owned_session.parent_start_token
        or _process_start_token(owned_session.parent_pid) != owned_session.parent_start_token
    ):
        _mark_session_unknown(session.pk, "child_cleanup_unproven")
        return False
    try:
        session, _execution = _execution_session_fence(owned_session.execution_id, owned_session.id, closing=True)
        if session.status != "active":
            raise AppiumExecutionError("The Appium execution session is not active")
        driver = attach_driver(session)
        quit_driver(driver)
    except AppiumOutcomeUnknown:
        _mark_session_unknown(session.pk, "appium_close_outcome_unknown")
        return False
    except Exception:
        _mark_session_unknown(session.pk, "appium_close_outcome_unknown")
        return False

    with transaction.atomic(durable=True):
        device = AppDevice.objects.select_for_update().filter(pk=session.device_id).first()
        current = AppiumSession.objects.select_for_update().filter(pk=session.pk).first()
        execution = AppTestExecution.objects.select_for_update().filter(pk=owned_session.execution_id).first()
        if (
            device is None
            or current is None
            or execution is None
            or device.locked_by_id != current.user_id
            or device.lease_generation != owned_session.lease_generation
            or device.device_action_epoch != current.device_action_epoch
            or not device.device_action_pending
            or execution.device_id != device.pk
            or execution.user_id != current.user_id
            or execution.device_lease_generation != owned_session.lease_generation
            or execution.device_action_pending is not True
            or execution.status != "running"
            or current.status != "active"
            or current.parent_pid != owned_session.parent_pid
            or current.parent_start_token != owned_session.parent_start_token
            or _process_start_token(owned_session.parent_pid) != owned_session.parent_start_token
        ):
            _mark_session_unknown(session.pk, "execution_cleanup_fence_changed")
            return False
        current.status = "closed"
        current.closed_at = timezone.now()
        current.parent_cleanup_proven_at = timezone.now()
        current.remote_session_id = ""
        current.save(update_fields=[
            "status", "closed_at", "parent_cleanup_proven_at", "remote_session_id", "updated_at",
        ])
        device.device_action_pending = False
        device.save(update_fields=["device_action_pending", "updated_at"])
        execution.device_action_pending = False
        execution.save(update_fields=["device_action_pending", "updated_at"])
    return True


__all__ = [
    "AppiumExecutionError",
    "ExecutionSessionHandle",
    "attach_execution_session",
    "finish_execution_session",
    "guard_execution_session",
    "preflight_execution_session",
    "start_execution_session",
]
