"""Deadline-bounded CPU work with one admitted call and no pending queue.

A timed-out daemon may finish later. Its admission stays held until it exits,
so repeated timeouts cannot accumulate workers. Callbacks must never access a
live device; capture device data synchronously through a timed adapter first.
"""
import math
import threading
import time


class InferenceTimeout(TimeoutError):
    pass


class InferenceBusy(RuntimeError):
    pass


class BoundedInference:
    def __init__(self, name):
        self._name = name
        self._admission = threading.Lock()

    def run(self, infer, deadline):
        if not math.isfinite(deadline) or time.monotonic() >= deadline:
            raise InferenceTimeout('Inference exceeded its deadline')
        if not self._admission.acquire(blocking=False):
            raise InferenceBusy('A previous inference is still running')

        completed = threading.Event()
        result = {}

        def work():
            try:
                result['value'] = infer()
            except Exception as error:
                result['error'] = error
            finally:
                self._admission.release()
                completed.set()

        try:
            threading.Thread(target=work, name=self._name, daemon=True).start()
        except Exception:
            self._admission.release()
            raise RuntimeError('Unable to start bounded inference') from None

        if not completed.wait(max(0.0, deadline - time.monotonic())):
            raise InferenceTimeout('Inference exceeded its deadline')
        if time.monotonic() >= deadline:
            raise InferenceTimeout('Inference exceeded its deadline')
        if 'error' in result:
            raise result['error']
        return result.get('value')
