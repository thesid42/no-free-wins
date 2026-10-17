"""Labeled fault injection for live drills: fail chosen calls before any request, then delegate.

An injected failure raises ModelError with a visible "injected fault: " prefix and tokens=0
without touching the inner client: no network, nothing billed. Logs hold no message content.
"""

import threading

from .models import ModelError

INJECTED_PREFIX = "injected fault: "
MAX_FAULTS = 3
MAX_DRILL_FAILURES = 2  # primary + retry fail, so the fallback model is the one that recovers


def _check_count(value, name, limit):
    if type(value) is not int or not 1 <= value <= limit:
        raise ValueError(f"{name} must be an integer from 1 to {limit}")


class FaultInjectingClient:
    def __init__(self, inner, *, faults):
        if type(faults) is not dict or not faults:
            raise ValueError("faults must be a non-empty dict of model_id -> failure count")
        for model_id, count in faults.items():
            if type(model_id) is not str or not model_id:
                raise ValueError("fault model ids must be non-empty strings")
            _check_count(count, f"faults[{model_id!r}]", MAX_FAULTS)
        self.inner = inner
        self._remaining = dict(faults)
        self._calls = {}
        self.log = []
        self._lock = threading.Lock()  # counters are shared by parallel callers

    @property
    def injected_total(self):
        return sum(1 for entry in self.log if entry["injected"])

    def _inject(self, model_id):
        """Record one call; raise the labeled error while failures remain for this model."""
        with self._lock:
            number = self._calls[model_id] = self._calls.get(model_id, 0) + 1
            injected = self._remaining.get(model_id, 0) > 0
            self.log.append({"model_id": model_id, "call_index": number, "injected": injected})
            if injected:
                self._remaining[model_id] -= 1
        if injected:
            raise ModelError(INJECTED_PREFIX + f"{model_id} call {number} "
                             "(labeled drill, no request sent)", tokens=0)

    def complete(self, model_id, role, messages, max_tokens):
        self._inject(model_id)
        return self.inner.complete(model_id, role, messages, max_tokens)

    def __getattr__(self, name):
        # Only reached for missing attributes: expose optional inner methods only if they exist.
        if name in ("complete_with_timeout", "list_models") and "inner" in self.__dict__:
            target = getattr(self.inner, name, None)
            if callable(target):
                if name == "list_models":
                    return target

                def complete_with_timeout(model_id, role, messages, max_tokens, timeout_seconds):
                    self._inject(model_id)
                    return target(model_id, role, messages, max_tokens, timeout_seconds)
                return complete_with_timeout
        raise AttributeError(name)


def drill_plan(orders, role="worker_a", failures=2):
    """faults mapping for FaultInjectingClient: fail the role's primary model `failures` times."""
    if type(orders) is not dict or role not in orders:
        raise ValueError(f"orders has no role {role!r}")
    _check_count(failures, "failures", MAX_DRILL_FAILURES)
    return {orders[role].primary: failures}


def drill_annotation(model_id, failures):
    """Scalar-only ledger annotation labeling the drill (accepted by the orchestrator)."""
    if type(model_id) is not str or not model_id:
        raise ValueError("model_id must be a non-empty string")
    _check_count(failures, "failures", MAX_FAULTS)
    return {"type": "fault_injection",
            "label": f"labeled drill: first {failures} calls to {model_id} fail before any "
                     "request; fallback must recover",
            "model_id": model_id, "failures": failures}
