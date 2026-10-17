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
                       before_attempt=None, after_attempt=None):
    """Return (parsed, attempt log). Logs hold no message content. Malformed output uses an attempt.

    before_attempt runs before reservation; its remaining seconds reach timeout-aware clients.
    after_attempt checks the deadline after each reply, including errors. Unknown failed usage
    retains its reservation. Reported usage and cost overruns stop before parsing a verdict.
    Raised checks carry the attempts so far in exc.attempts."""
    attempts = []
    def check_after():
        try:
            budget.check_limits()
            if after_attempt is not None:
                after_attempt()
        except Exception as exc:
            exc.attempts = attempts
            raise

    sequence = ((order.primary, "primary"), (order.primary, "retry"), (order.fallback, "fallback"))
    for number, (model_id, kind) in enumerate(sequence, 1):
        remaining = None
        if before_attempt is not None:
            try:
                remaining = before_attempt()
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
            timed_complete = getattr(client, "complete_with_timeout", None)
            if remaining is not None and callable(timed_complete):
                reply = timed_complete(model_id, role, messages, max_tokens, remaining)
            else:
                reply = client.complete(model_id, role, messages, max_tokens)
        except ModelError as exc:
            # Unknown usage may have been billed: retain the full reservation.
            charged = exc.tokens if exc.tokens is not None else reservation.tokens
            budget.settle(reservation, charged)
            entry.update(outcome="model_error", detail=str(exc)[:200], tokens=charged,
                         usage_known=exc.tokens is not None)
            check_after()
            continue
        budget.settle(reservation, reply.tokens)
        entry["tokens"] = reply.tokens
        check_after()
        try:
            return parse(reply.text), attempts
        except MalformedOutput as exc:
            entry.update(outcome="malformed", detail=str(exc)[:200])
    raise OrderFailed(attempts)
