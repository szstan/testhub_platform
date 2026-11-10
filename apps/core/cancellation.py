"""Cooperative cancellation scope for source executions."""

from __future__ import annotations

from contextvars import ContextVar, Token
from contextlib import contextmanager
from threading import Event
from typing import Callable, Iterator


CancelCheck = Callable[[], None]
_active_scope: ContextVar['ExecutionCancellationScope | None'] = ContextVar(
    'execution_cancellation_scope', default=None
)


class ExecutionCancellationScope:
    """Compose lease checks with an explicit local cancellation signal."""

    def __init__(self, *checks: CancelCheck | None):
        self._checks = tuple(check for check in checks if check is not None)
        self._cancelled = Event()
        self._reason = 'execution cancelled'

    def cancel(self, reason='execution cancelled'):
        self._reason = str(reason or 'execution cancelled')
        self._cancelled.set()

    def raise_if_cancelled(self):
        if self._cancelled.is_set():
            raise RuntimeError(self._reason)
        for check in self._checks:
            check()


def combine_cancel_checks(*checks: CancelCheck | None) -> CancelCheck:
    scope = ExecutionCancellationScope(*checks)
    return scope.raise_if_cancelled


@contextmanager
def execution_cancellation_scope(*checks: CancelCheck | None) -> Iterator[ExecutionCancellationScope]:
    scope = ExecutionCancellationScope(*checks)
    token: Token = _active_scope.set(scope)
    try:
        yield scope
    finally:
        _active_scope.reset(token)


def raise_if_cancelled():
    """Check the current execution scope when called from a source utility."""
    scope = _active_scope.get()
    if scope is not None:
        scope.raise_if_cancelled()


__all__ = [
    'ExecutionCancellationScope',
    'combine_cancel_checks',
    'execution_cancellation_scope',
    'raise_if_cancelled',
]
