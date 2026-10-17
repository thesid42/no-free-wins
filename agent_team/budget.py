"""Reserve-before-call inference budget: attempts, tokens, cost (one task scope).

Token counts are TOTAL (prompt + completion). Prices are USD per 1k total tokens; the
default is unpriced (0.0) for fixtures, so live runs must supply `prices` or
`usd_per_1k_tokens` or the cost cap does nothing (snapshot()["priced"] reports this).
"""

from dataclasses import dataclass
import itertools


class BudgetExhausted(RuntimeError):
    pass


def estimate_prompt_tokens(messages):
    """Conservative prompt size estimate (about 3 characters per token)."""
    return sum(len(m["content"]) for m in messages) // 3 + 1


@dataclass(frozen=True)
class Reservation:
    reservation_id: int
    tokens: int
    price: float = 0.0


class Budget:
    def __init__(self, *, max_attempts=60, max_tokens=180_000, max_cost_usd=25.0,
                 task_attempts=12, usd_per_1k_tokens=0.0, prices=None):
        self.max_attempts, self.max_tokens = max_attempts, max_tokens
        self.max_cost_usd, self.task_attempts = max_cost_usd, task_attempts
        self.usd_per_1k_tokens, self.prices = usd_per_1k_tokens, dict(prices or {})
        self.attempts_used, self.tokens_used, self.cost_used = 0, 0, 0.0
        self._open, self._ids = {}, itertools.count(1)

    def _price(self, model_id):
        return self.prices.get(model_id, self.usd_per_1k_tokens)

    def reserve(self, tokens, *, model_id=None):
        """Count the attempt and hold tokens/cost now; raises before any model call."""
        if type(tokens) is not int or tokens <= 0:
            raise ValueError("tokens must be a positive integer")
        price = self._price(model_id)
        if self.attempts_used + 1 > self.max_attempts:
            raise BudgetExhausted("attempt budget exhausted")
        if self.attempts_used + 1 > self.task_attempts:
            raise BudgetExhausted("task attempt budget exhausted")
        if self.tokens_used >= self.max_tokens or self.tokens_used + tokens > self.max_tokens:
            raise BudgetExhausted("token budget exhausted")
        if self.cost_used + tokens * price / 1000 > self.max_cost_usd:
            raise BudgetExhausted("cost budget exhausted")
        reservation = Reservation(next(self._ids), tokens, price)
        self.attempts_used += 1
        self.tokens_used += tokens
        self.cost_used += tokens * price / 1000
        self._open[reservation.reservation_id] = reservation
        return reservation

    def settle(self, reservation, actual_tokens):
        """Replace the reservation with actual usage (refund unused; overrun is recorded)."""
        if type(actual_tokens) is not int or actual_tokens < 0:
            raise ValueError("actual tokens must be a non-negative integer")
        if self._open.pop(reservation.reservation_id, None) is None:
            raise ValueError("unknown or already settled reservation")
        self.tokens_used += actual_tokens - reservation.tokens
        self.cost_used += (actual_tokens - reservation.tokens) * reservation.price / 1000

    def snapshot(self):
        priced = self.usd_per_1k_tokens > 0 or any(p > 0 for p in self.prices.values())
        return {"attempts_used": self.attempts_used, "tokens_used": self.tokens_used,
                "cost_usd": self.cost_used, "open_reservations": len(self._open),
                "overrun": self.tokens_used > self.max_tokens, "priced": priced,
                "max_attempts": self.max_attempts, "task_attempts": self.task_attempts,
                "max_tokens": self.max_tokens, "max_cost_usd": self.max_cost_usd}
