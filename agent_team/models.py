"""Model client protocol plus a scripted fixture client. No network, no vendor SDK."""

import copy
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelReply:
    """tokens is the TOTAL (prompt + completion) usage for the call."""
    text: str
    tokens: int


class ModelError(RuntimeError):
    pass


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

    def complete(self, model_id, role, messages, max_tokens):
        self.calls.append((model_id, role, copy.deepcopy(messages)))
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
