"""State and error contracts for the unified execution domain.

The legacy TestPlan/TestRun models predate this contract and intentionally keep
 their own statuses.  These values are used only by the additive execution
 domain models below.
"""

from django.db import models


class ExecutionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    CANCEL_REQUESTED = "cancel_requested", "Cancel requested"
    CANCELLED = "cancelled", "Cancelled"
    PASSED = "passed", "Passed"
    SUCCEEDED = "succeeded", "Succeeded"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    BLOCKED = "blocked", "Blocked"
    UNSUPPORTED = "unsupported", "Unsupported"


class AttemptStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    CANCEL_REQUESTED = "cancel_requested", "Cancel requested"
    CANCELLED = "cancelled", "Cancelled"
    PASSED = "passed", "Passed"
    SUCCEEDED = "succeeded", "Succeeded"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    BLOCKED = "blocked", "Blocked"
    UNSUPPORTED = "unsupported", "Unsupported"


class StepStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    PASSED = "passed", "Passed"
    SUCCEEDED = "succeeded", "Succeeded"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    BLOCKED = "blocked", "Blocked"
    SKIPPED = "skipped", "Skipped"


class ErrorType(models.TextChoices):
    ASSERTION = "assertion", "Assertion"
    TIMEOUT = "timeout", "Timeout"
    CONFIGURATION = "configuration", "Configuration"
    INFRASTRUCTURE = "infrastructure", "Infrastructure"
    PERMISSION = "permission", "Permission"
    CANCELLED = "cancelled", "Cancelled"
    UNSUPPORTED = "unsupported", "Unsupported"
    UNKNOWN = "unknown", "Unknown"


# A transition to the same state is treated as an idempotent no-op.  The
# explicit map keeps executor adapters from inventing incompatible states.
JOB_TRANSITIONS = {
    ExecutionStatus.PENDING: {
        ExecutionStatus.PENDING,
        ExecutionStatus.QUEUED,
        ExecutionStatus.CANCEL_REQUESTED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.UNSUPPORTED,
    },
    ExecutionStatus.QUEUED: {
        ExecutionStatus.QUEUED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCEL_REQUESTED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.UNSUPPORTED,
    },
    ExecutionStatus.RUNNING: {
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCEL_REQUESTED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.PASSED,
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.BLOCKED,
    },
    ExecutionStatus.CANCEL_REQUESTED: {
        ExecutionStatus.CANCEL_REQUESTED,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.CANCELLED: {
        ExecutionStatus.CANCELLED,
        ExecutionStatus.QUEUED,
    },
    ExecutionStatus.PASSED: {ExecutionStatus.PASSED},
    ExecutionStatus.SUCCEEDED: {ExecutionStatus.SUCCEEDED},
    ExecutionStatus.COMPLETED: {ExecutionStatus.COMPLETED},
    ExecutionStatus.FAILED: {
        ExecutionStatus.FAILED,
        ExecutionStatus.QUEUED,
    },
    ExecutionStatus.BLOCKED: {
        ExecutionStatus.BLOCKED,
        ExecutionStatus.QUEUED,
    },
    ExecutionStatus.UNSUPPORTED: {
        ExecutionStatus.UNSUPPORTED,
        ExecutionStatus.QUEUED,
    },
}

# Attempts follow the same lifecycle but may not be queued by an adapter that
# starts work immediately.  Keeping this map separate leaves room for a
# future adapter to enforce attempt-specific rules without changing jobs.
ATTEMPT_TRANSITIONS = {
    AttemptStatus.PENDING: {
        AttemptStatus.PENDING,
        AttemptStatus.QUEUED,
        AttemptStatus.RUNNING,
        AttemptStatus.CANCEL_REQUESTED,
        AttemptStatus.CANCELLED,
        AttemptStatus.UNSUPPORTED,
    },
    AttemptStatus.QUEUED: {
        AttemptStatus.QUEUED,
        AttemptStatus.RUNNING,
        AttemptStatus.CANCEL_REQUESTED,
        AttemptStatus.CANCELLED,
        AttemptStatus.UNSUPPORTED,
    },
    AttemptStatus.RUNNING: {
        AttemptStatus.RUNNING,
        AttemptStatus.CANCEL_REQUESTED,
        AttemptStatus.CANCELLED,
        AttemptStatus.PASSED,
        AttemptStatus.SUCCEEDED,
        AttemptStatus.COMPLETED,
        AttemptStatus.FAILED,
        AttemptStatus.BLOCKED,
    },
    AttemptStatus.CANCEL_REQUESTED: {
        AttemptStatus.CANCEL_REQUESTED,
        AttemptStatus.CANCELLED,
    },
    AttemptStatus.CANCELLED: {
        AttemptStatus.CANCELLED,
        AttemptStatus.QUEUED,
    },
    AttemptStatus.PASSED: {AttemptStatus.PASSED},
    AttemptStatus.SUCCEEDED: {AttemptStatus.SUCCEEDED},
    AttemptStatus.COMPLETED: {AttemptStatus.COMPLETED},
    AttemptStatus.FAILED: {
        AttemptStatus.FAILED,
        AttemptStatus.QUEUED,
    },
    AttemptStatus.BLOCKED: {
        AttemptStatus.BLOCKED,
        AttemptStatus.QUEUED,
    },
    AttemptStatus.UNSUPPORTED: {
        AttemptStatus.UNSUPPORTED,
        AttemptStatus.QUEUED,
    },
}

STEP_TRANSITIONS = {
    StepStatus.PENDING: {
        StepStatus.PENDING,
        StepStatus.RUNNING,
        StepStatus.SKIPPED,
    },
    StepStatus.RUNNING: {
        StepStatus.RUNNING,
        StepStatus.PASSED,
        StepStatus.SUCCEEDED,
        StepStatus.COMPLETED,
        StepStatus.FAILED,
        StepStatus.BLOCKED,
        StepStatus.SKIPPED,
    },
    StepStatus.PASSED: {StepStatus.PASSED},
    StepStatus.SUCCEEDED: {StepStatus.SUCCEEDED},
    StepStatus.COMPLETED: {StepStatus.COMPLETED},
    StepStatus.FAILED: {StepStatus.FAILED},
    StepStatus.BLOCKED: {StepStatus.BLOCKED},
    StepStatus.SKIPPED: {StepStatus.SKIPPED},
}


class InvalidExecutionTransition(ValueError):
    """Raised when a domain object attempts an illegal state transition."""


def _value(status):
    return getattr(status, "value", status)


def validate_transition(current, target, transitions=JOB_TRANSITIONS):
    """Validate and return the target status as a plain string."""
    current_value = _value(current)
    target_value = _value(target)
    allowed = transitions.get(current_value, set())
    if target_value not in allowed:
        raise InvalidExecutionTransition(
            f"Cannot transition execution from {current_value!r} to {target_value!r}"
        )
    return target_value


def can_transition(current, target, transitions=JOB_TRANSITIONS):
    """Return whether a status transition is allowed without raising."""
    try:
        validate_transition(current, target, transitions)
    except InvalidExecutionTransition:
        return False
    return True


# Backwards-friendly names for callers that prefer a domain-specific prefix.
ExecutionJobStatus = ExecutionStatus
ExecutionAttemptStatus = AttemptStatus
ExecutionStepStatus = StepStatus
ExecutionErrorType = ErrorType

__all__ = [
    "ATTEMPT_TRANSITIONS",
    "ErrorType",
    "ExecutionAttemptStatus",
    "ExecutionErrorType",
    "ExecutionJobStatus",
    "ExecutionStatus",
    "ExecutionStepStatus",
    "InvalidExecutionTransition",
    "JOB_TRANSITIONS",
    "STEP_TRANSITIONS",
    "AttemptStatus",
    "StepStatus",
    "can_transition",
    "validate_transition",
]
