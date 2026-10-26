"""Celery entry points for the unified execution domain."""

from .retry_outbox import consume_execution_retry_outbox

__all__ = ['consume_execution_retry_outbox']
