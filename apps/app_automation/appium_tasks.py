"""Short-lived Appium operations isolated on the interactive Celery queue."""

from __future__ import annotations

from celery import shared_task

from .appium_models import AppiumOperation
from .appium_runtime import (
    AppiumClientUnavailable,
    AppiumConfigurationError,
    AppiumInputError,
    AppiumOutcomeUnknown,
    AppiumRuntimeError,
    attach_driver,
    click_element,
    create_driver,
    endpoint_for_device,
    inspect_screen,
    locate_element,
    quit_driver,
)
from .appium_sessions import (
    finish_operation,
    fenced_session_snapshot,
    resolve_operation_element,
)


def _operation_context(operation_id):
    operation = AppiumOperation.objects.filter(pk=operation_id).select_related("session").first()
    if operation is None:
        return None
    return fenced_session_snapshot(operation.session_id, operation.pk)


def _operation_error_code(error):
    return getattr(error, "code", "appium_operation_failed")


def _failure_result(operation, *, unknown=False, error_code="appium_operation_failed"):
    state = "unknown" if unknown else "failed"
    finish_operation(
        operation.pk,
        operation.session_id,
        status=state,
        error_code=error_code,
    )
    return {
        "success": False,
        "status": state,
        "error": error_code,
        "recovery_required": unknown,
    }


def _start(operation, session, device):
    try:
        if endpoint_for_device(device) != session.appium_endpoint:
            raise AppiumConfigurationError("The configured Appium endpoint changed")
        driver = create_driver(device, session.package, system_port=session.system_port)
    except (AppiumClientUnavailable, AppiumConfigurationError, AppiumInputError) as exc:
        return _failure_result(operation, error_code=_operation_error_code(exc))
    except AppiumOutcomeUnknown as exc:
        return _failure_result(operation, unknown=True, error_code=_operation_error_code(exc))
    except Exception:
        return _failure_result(operation, unknown=True, error_code="appium_session_start_unknown")

    private_result = {
        "remote_session_id": driver.session_id,
        "capabilities": dict(session.capabilities),
    }
    public_result = {
        "success": True,
        "status": "active",
        "session_id": str(session.pk),
    }
    try:
        completed = finish_operation(
            operation.pk, operation.session_id, status="succeeded", result=private_result,
        )
    except Exception:
        completed = False
    if not completed:
        try:
            quit_driver(driver)
        except AppiumOutcomeUnknown:
            return _failure_result(
                operation, unknown=True, error_code="appium_session_start_persistence_unknown",
            )
        return _failure_result(
            operation, unknown=True, error_code="appium_session_start_persistence_unknown",
        )
    return public_result


def _perform(operation, session, device):
    try:
        if endpoint_for_device(device) != session.appium_endpoint:
            raise AppiumConfigurationError("The configured Appium endpoint changed")
    except AppiumConfigurationError as exc:
        return _failure_result(
            operation,
            unknown=operation.action in {"click", "close"},
            error_code=_operation_error_code(exc),
        )
    try:
        driver = attach_driver(session)
    except AppiumOutcomeUnknown as exc:
        return _failure_result(
            operation,
            unknown=operation.action in {"click", "close"},
            error_code=_operation_error_code(exc),
        )
    except AppiumRuntimeError as exc:
        return _failure_result(operation, error_code=_operation_error_code(exc))

    try:
        if operation.action in {"screenshot", "tree"}:
            inspected = inspect_screen(driver, package_name=session.package.package_name)
            result = {"success": True, **inspected} if operation.action == "tree" else {
                "success": True, "screenshot": inspected["screenshot"],
            }
            state = "succeeded"
        elif operation.action in {"locate", "click"}:
            locator = resolve_operation_element(session, operation)
            if operation.action == "locate":
                result = {
                    "success": True,
                    **({"element_id": operation.payload["element_id"]} if "element_id" in operation.payload else {}),
                    **locate_element(
                        driver,
                        locator["strategy"],
                        locator["value"],
                        container=locator.get("container"),
                        package_name=locator["package_name"],
                    ),
                }
                state = "succeeded"
            else:
                click_element(
                    driver,
                    locator["strategy"],
                    locator["value"],
                    container=locator.get("container"),
                    package_name=locator["package_name"],
                )
                result = {
                    "success": True,
                    "action_outcome": "completed",
                    "recovery_required": False,
                    "recorded_step": {
                        "type": "click",
                        "element_id": operation.payload["element_id"],
                    },
                }
                state = "succeeded"
        elif operation.action == "close":
            quit_driver(
                driver,
                expected_endpoint=session.appium_endpoint,
                expected_session_id=session.remote_session_id,
            )
            result = {"success": True, "status": "closed"}
            state = "succeeded"
        else:
            return _failure_result(operation, error_code="unsupported_operation")
    except AppiumOutcomeUnknown as exc:
        return _failure_result(
            operation,
            unknown=operation.action in {"click", "close"},
            error_code=_operation_error_code(exc),
        )
    except (AppiumInputError, AppiumRuntimeError) as exc:
        return _failure_result(operation, error_code=_operation_error_code(exc))
    except Exception:
        return _failure_result(
            operation,
            unknown=operation.action in {"click", "close"},
            error_code=(
                "appium_click_outcome_unknown"
                if operation.action == "click"
                else "appium_operation_failed"
            ),
        )

    if not finish_operation(operation.pk, operation.session_id, status=state, result=result):
        return {
            "success": False,
            "status": "unknown",
            "error": "ownership_fence_changed",
            "recovery_required": True,
        }
    return result


def _run_operation(operation_id):
    claimed = _operation_context(operation_id)
    if claimed is None:
        return {"success": False, "status": "not_dispatched", "error": "operation_not_claimed"}
    session, operation, device = claimed
    if operation.action == "start":
        return _start(operation, session, device)
    return _perform(operation, session, device)


@shared_task(name="apps.app_automation.appium_tasks.start_appium_session_task", ignore_result=True)
def start_appium_session_task(operation_id):
    _run_operation(operation_id)


@shared_task(name="apps.app_automation.appium_tasks.run_appium_operation_task", ignore_result=True)
def run_appium_operation_task(operation_id):
    _run_operation(operation_id)


@shared_task(name="apps.app_automation.appium_tasks.close_appium_session_task", ignore_result=True)
def close_appium_session_task(operation_id):
    _run_operation(operation_id)
