"""One model order: primary, one retry, one fallback. Flat, at most three attempts."""

from dataclasses import dataclass

from .budget import BudgetExhausted, estimate_prompt_tokens
from .models import MalformedOutput, ModelError


@dataclass(frozen=True)
class ModelOrder:
    primary: str
    fallback: str

    def __post_init__(self):
        if not self.primary or not self.fallback or self.primary == self.fallback:
            raise ValueError("primary and fallback must be distinct model ids")


class OrderFailed(RuntimeError):
    def __init__(self, attempts):
        super().__init__(f"all {len(attempts)} attempts failed")
        self.attempts = attempts


def call_with_fallback(order, client, budget, *, role, messages, parse, max_tokens=4096,
                       before_attempt=None):
    """Return (parsed, attempt log). Logs hold no message content. Malformed output uses an attempt.

    before_attempt, if given, runs before every attempt's reservation; if it raises, the
    attempts so far are attached as exc.attempts and the exception propagates."""
    attempts = []
    sequence = ((order.primary, "primary"), (order.primary, "retry"), (order.fallback, "fallback"))
    for number, (model_id, kind) in enumerate(sequence, 1):
        if before_attempt is not None:
            try:
                before_attempt()
            except Exception as exc:
                exc.attempts = attempts
                raise
        entry = {"attempt": number, "model_id": model_id, "kind": kind,
                 "outcome": "ok", "tokens": 0, "detail": ""}
        attempts.append(entry)
        try:
            reservation = budget.reserve(estimate_prompt_tokens(messages) + max_tokens,
                                       model_id=model_id)
        except BudgetExhausted as exc:
            entry.update(outcome="budget_exhausted", detail=str(exc)[:200])
            exc.attempts = attempts
            raise
        try:
            reply = client.complete(model_id, role, messages, max_tokens)
        except ModelError as exc:
            budget.settle(reservation, 0)
            entry.update(outcome="model_error", detail=str(exc)[:200])
            continue
        budget.settle(reservation, reply.tokens)
        entry["tokens"] = reply.tokens
        try:
            return parse(reply.text), attempts
        except MalformedOutput as exc:
            entry.update(outcome="malformed", detail=str(exc)[:200])
    raise OrderFailed(attempts)
