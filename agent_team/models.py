"""Model client protocol plus a scripted fixture client. No network, no vendor SDK."""

import copy
from dataclasses import dataclass
import threading
from typing import Protocol


@dataclass(frozen=True)
class ModelReply:
    """tokens is the TOTAL (prompt + completion) usage for the call."""
    text: str
    tokens: int


class ModelError(RuntimeError):
    def __init__(self, message, *, tokens=None):
        super().__init__(message)
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("error usage must be a non-negative integer or unknown")
        self.tokens = tokens


class MalformedOutput(ValueError):
    pass


class ModelClient(Protocol):
    def complete(self, model_id: str, role: str, messages: list[dict],
                 max_tokens: int) -> ModelReply: ...


class FixtureModelClient:
    """script: model_id -> queue of str | ModelReply | Exception, popped in order."""

    def __init__(self, script):
        self.script = {model: list(queue) for model, queue in script.items()}
        self.calls = []
        self._lock = threading.Lock()  # parallel workers/verifiers may call concurrently

    def complete(self, model_id, role, messages, max_tokens):
        recorded = (model_id, role, copy.deepcopy(messages))
        with self._lock:
            self.calls.append(recorded)
            queue = self.script.get(model_id)
            if not queue:
                raise ModelError("fixture exhausted")
            item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if type(item) is str:
            return ModelReply(item, len(item) // 4 + 1)
        if type(item) is ModelReply:
            return item
        raise TypeError("unsupported fixture item")
