"""Bounded B orchestration: plan, two workers, screen, verifier, acceptance, terminal.

One controller owns the hash-chained official ledger. Harness and model client are injected.
Model text never decides acceptance: only harness response fields do. Events carry ids,
hashes, statuses and metrics, never messages or candidate source.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import time
import traceback
import uuid

from optimizer_core.canonical import canonical_json, digest
from optimizer_core.ledger import append_event

from . import contract, roles
from .budget import BudgetExhausted
from .calls import ModelOrder, OrderFailed, call_with_fallback
from .playbook import Playbook

TERMINAL_STATES = {"verified_done", "simulation_completed", "failed_plan", "failed_missing_strategy",
                   "failed_no_candidates", "failed_budget", "failed_deadline", "failed_evidence",
                   "failed_infrastructure", "rounds_exhausted"}
ORDER_KEYS = {"planner", "worker_a", "worker_b", "verifier"}
MAX_DEADLINE_SECONDS = 1800
# A's real stage bounds (the harness _stage budgets and the 60 s trial floor): 180 s
# correctness + 5 pairs + margin for screen, 600 s acceptance + margin. Real-harness runs keep these.
DEFAULT_MIN_HARNESS_SECONDS = {"screen": 240, "acceptance": 840}
ANNOTATION_TYPES = {"fault_injection", "simulation_label"}
RESERVED_EVENT_KEYS = {"type", "run_id", "utc", "round", "kind", "rnd"}  # incl. _log params


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _check_annotation(item):
    """Labeled drill/simulation notes only; they can never impersonate controller events."""
    if type(item) is not dict or item.get("type") not in ANNOTATION_TYPES:
        raise ValueError("annotation type must be fault_injection or simulation_label")
    for key, value in item.items():
        if type(key) is not str or (key in RESERVED_EVENT_KEYS and key != "type"):
            raise ValueError("annotation uses a reserved or non-string key")
        if type(value) not in (str, int, float, bool) or (type(value) is float and not math.isfinite(value)):
            raise ValueError("annotation values must be str, int, float or bool")


@dataclass
class RunResult:
    run_id: str
    state: str
    rounds: int
    best_response: dict | None
    ledger_path: str
    ledger_final_sha256: str
    budget: dict
    events: list[dict]


class _Terminal(Exception):
    def __init__(self, state, reason, attempts=None):
        super().__init__(state)
        self.state, self.reason, self.attempts = state, reason, attempts


class Orchestrator:
    def __init__(self, *, evaluate, client, budget, ledger_path, session_id, orders,
                 deadline_seconds, max_rounds=3, screen_bar=3.0, clock=time.monotonic,
                 utcnow=_utcnow, new_id=lambda: uuid.uuid4().hex,
                 task_summary=roles.TASK_SUMMARY, playbook=None, annotations=(),
                 min_harness_seconds=None, preflight=None):
        if not callable(evaluate):
            raise ValueError("evaluate must be callable")
        if (type(orders) is not dict or set(orders) != ORDER_KEYS or
                not all(type(order) is ModelOrder for order in orders.values())):
            raise ValueError("orders must map planner/worker_a/worker_b/verifier to ModelOrder")
        if type(session_id) is not str or not 1 <= len(session_id) <= 128:
            raise ValueError("session_id must be a 1-128 character string")
        if not _number(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive and finite")
        if type(max_rounds) is not int or max_rounds < 1:
            raise ValueError("max_rounds must be a positive integer")
        if not _number(screen_bar) or screen_bar <= 0:
            raise ValueError("screen_bar must be positive and finite")
        if type(annotations) not in (list, tuple):
            raise ValueError("annotations must be a list")
        for item in annotations:
            _check_annotation(item)
        floors = DEFAULT_MIN_HARNESS_SECONDS if min_harness_seconds is None else min_harness_seconds
        if (type(floors) is not dict or set(floors) != {"screen", "acceptance"} or
                not all(_number(v) and v >= 0 for v in floors.values())):
            raise ValueError("min_harness_seconds needs non-negative screen and acceptance floors")
        if preflight is not None and not callable(preflight):
            raise ValueError("preflight must be callable")
        self.min_harness_seconds, self._preflight = dict(floors), preflight
        self._evaluate_fn, self.client, self.budget = evaluate, client, budget
        self.ledger_path, self.session_id, self.orders = Path(ledger_path), session_id, orders
        self.deadline_seconds = min(deadline_seconds, MAX_DEADLINE_SECONDS)
        self.max_rounds, self.screen_bar = max_rounds, screen_bar
        self._clock, self._utcnow, self._new_id = clock, utcnow, new_id
        self.task_summary = task_summary
        self.playbook = playbook if playbook is not None else Playbook()
        self.annotations = [dict(item) for item in annotations]
        self.run_id = f"run-{new_id()}"
        self.events, self._responses, self._codes = [], [], []
        self._seen, self._workers = {}, []
        self._forwarded = self._best_response = None
        self._rounds = 0
        self._start = self._deadline = None

    # -- helpers -----------------------------------------------------------------------
    def _log(self, kind, rnd, **fields):
        event = {"type": kind, "run_id": self.run_id, "utc": self._utcnow(), "round": rnd, **fields}
        record = append_event(self.ledger_path, event)
        self.events.append(event)
        return record

    def _remaining(self):
        """Seconds to the monotonic deadline; terminal as soon as none remain."""
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise _Terminal("failed_deadline", "deadline reached before next call")
        return float(remaining)

    def _heartbeat(self, phase, rnd):
        self._log("heartbeat", rnd, phase=phase, elapsed_seconds=self._clock() - self._start,
                  budget=self.budget.snapshot())

    def _call(self, key, messages, parse):
        return call_with_fallback(self.orders[key], self.client, self.budget, role=key,
                                  messages=messages, parse=parse, before_attempt=self._remaining)

    def _harness(self, candidate, mode, rnd, verifier=None):
        """One deadline-checked, validated harness call; terminal on any infrastructure fault."""
        remaining = self._remaining()
        if remaining < self.min_harness_seconds[mode]:
            raise _Terminal("failed_deadline", f"insufficient time for {mode}")
        request = contract.build_request(
            request_id=f"req-{self._new_id()}", session_id=self.session_id, candidate=candidate,
            mode=mode, remaining_wall_seconds=remaining, verifier=verifier)
        try:
            response = self._evaluate_fn(request)
        except Exception as exc:
            self._log("harness_error", rnd, candidate_id=candidate["candidate_id"], mode=mode,
                      error_type=type(exc).__name__)
            raise _Terminal("failed_infrastructure", f"harness raised in {mode}") from exc
        try:
            contract.validate_response(response, request)
        except ValueError as exc:
            self._log("evidence_invalid", rnd, candidate_id=candidate["candidate_id"], mode=mode)
            raise _Terminal("failed_evidence", f"invalid {mode} response") from exc
        self._responses.append(response)
        return response

    def _result_fields(self, candidate, response):
        return {"candidate_id": candidate["candidate_id"], "candidate_sha256": candidate["sha256"],
                "status": response["status"], "origin": response["origin"],
                "accepted": response["accepted"], "correctness_passed": response["correctness_passed"],
                "evidence_ids": response["evidence_ids"],
                "response_sha256": digest(canonical_json(response))}

    # -- phases ------------------------------------------------------------------------
    def _plan(self):
        self._heartbeat("plan", 0)
        try:
            strategies, attempts = self._call("planner", roles.planner_messages(self.task_summary),
                                              roles.parse_plan)
        except OrderFailed as exc:
            raise _Terminal("failed_plan", "planner order failed", exc.attempts) from exc
        plan_bytes = canonical_json(strategies)
        (self.ledger_path.parent / "plan.json").write_bytes(plan_bytes)  # raw model text stays off-ledger
        summary = [{"strategy_id": str(item.get("strategy_id"))[:64],
                    "description_sha256": digest(str(item.get("description")).encode("utf-8"))}
                   for item in strategies if type(item) is dict]
        distinct = roles.distinct_strategies(strategies)
        self._log("plan", 0, distinct=distinct, attempts=attempts, strategies=summary,
                  plan_sha256=digest(plan_bytes))
        if not distinct:
            raise _Terminal("failed_missing_strategy", "plan lacks two distinct strategies")
        self._workers = list(zip(("worker_a", "worker_b"), strategies[:2]))
        for worker, strategy in self._workers:
            order = self.orders[worker]
            self._log("worker_order", 0, worker=worker, strategy_id=strategy["strategy_id"][:64],
                      description_sha256=digest(strategy["description"].encode("utf-8")),
                      primary=order.primary, fallback=order.fallback)

    def _work(self, rnd):
        feedback = roles.public_feedback(self._responses) + [dict(c) for c in self._codes]
        new, failed = [], 0
        self._heartbeat("work", rnd)
        for worker, strategy in self._workers:
            messages = roles.worker_messages(self.task_summary, strategy, feedback)
            try:
                source, attempts = self._call(worker, messages, roles.parse_candidate)
            except OrderFailed as exc:
                failed += 1
                self._log("worker_failed", rnd, worker=worker, strategy_id=strategy["strategy_id"][:64],
                          attempts=exc.attempts)
                continue
            candidate = contract.make_candidate(f"{worker}-r{rnd}", source)
            if candidate["sha256"] in self._seen:
                self._log("duplicate_candidate", rnd, worker=worker,
                          candidate_id=candidate["candidate_id"], candidate_sha256=candidate["sha256"],
                          duplicate_of=self._seen[candidate["sha256"]], attempts=attempts)
                continue
            self._seen[candidate["sha256"]] = candidate["candidate_id"]
            directory = self.ledger_path.parent / "candidates"
            directory.mkdir(parents=True, exist_ok=True)
            with open(directory / f"{candidate['candidate_id']}.py", "w", encoding="utf-8",
                      newline="") as stream:
                stream.write(source)
            self._log("candidate_created", rnd, worker=worker, strategy_id=strategy["strategy_id"][:64],
                      candidate_id=candidate["candidate_id"], candidate_sha256=candidate["sha256"],
                      attempts=attempts)
            new.append(candidate)
        if not new and rnd == 1 and failed == len(self._workers):
            raise _Terminal("failed_no_candidates", "both workers failed in round 1")
        return new

    def _screen(self, rnd, new):
        self._heartbeat("screen", rnd)
        best = None
        for candidate in new:
            response = self._harness(candidate, "screen", rnd)
            speedup = response["screen_median_speedup"]
            self._log("screen_result", rnd, screen_median_speedup=speedup,
                      **self._result_fields(candidate, response))
            if response["status"] == "budget_exhausted":
                raise _Terminal("failed_deadline", "harness budget exhausted in screen")
            if response["status"] == "infrastructure_error":
                raise _Terminal("failed_infrastructure", "screen status infrastructure_error")
            if (response["status"] == "screened" and response["correctness_passed"] and
                    speedup is not None and speedup >= self.screen_bar and
                    (best is None or speedup > best[2])):
                best = (candidate, response, speedup)
        if best is None:
            self._codes.append({"code": "screen_below_bar"})
            return None
        self._best_response = best[1]
        return best[0]

    def _verify(self, rnd, candidate):
        """Return the controller-minted verdict id, or None when rejected."""
        self._heartbeat("verify", rnd)
        sha = candidate["sha256"]
        fields = {"candidate_id": candidate["candidate_id"], "candidate_sha256": sha}
        self._forwarded = candidate
        try:
            verdict, attempts = self._call(
                "verifier", roles.verifier_messages(self.task_summary, candidate["source_utf8"], sha),
                lambda text: roles.parse_verdict(text, sha))
        except OrderFailed as exc:
            self._log("verifier_verdict", rnd, approved=False, verifier_failed=True,
                      attempts=exc.attempts, **fields)
            self._codes.append({"code": "verifier_rejected"})
            return None
        if verdict["approved"] is not True:
            self._log("verifier_verdict", rnd, approved=False, mismatch=bool(verdict.get("mismatch")),
                      attempts=attempts, **fields)
            self._codes.append({"code": "verifier_rejected"})
            return None
        verdict_id = f"verdict-{self._new_id()}"
        self._log("verifier_verdict", rnd, approved=True, mismatch=False, verdict_id=verdict_id,
                  attempts=attempts, **fields)
        return verdict_id

    def _accept(self, rnd, candidate, verdict_id):
        self._heartbeat("acceptance", rnd)
        response = self._harness(candidate, "acceptance", rnd,
                                 verifier={"approved": True, "evidence_ids": [verdict_id]})
        self._best_response = response
        self._log("acceptance_result", rnd, verdict_id=verdict_id, receipt_id=response["receipt_id"],
                  receipt_sha256=response["receipt_sha256"], acceptance=response["acceptance"],
                  **self._result_fields(candidate, response))
        status = response["status"]
        if contract.is_verified_done(response):
            raise _Terminal("verified_done", "measured receipted acceptance meets the bar")
        if (status == "simulated_acceptance" and response["origin"] == "synthetic" and
                response["accepted"] is False):
            raise _Terminal("simulation_completed", "synthetic simulation finished; not a verified result")
        if status == "budget_exhausted":
            raise _Terminal("failed_deadline", "harness budget exhausted in acceptance")
        if status == "infrastructure_error":
            raise _Terminal("failed_infrastructure", "acceptance status infrastructure_error")
        if status in ("bar_not_met", "rejected") and response["accepted"] is False:
            return  # public feedback from this response is forwarded next round
        raise _Terminal("failed_evidence", f"acceptance response not verifiable ({status})")

    def _run_preflight(self):
        if self._preflight is None:
            return
        try:
            self._preflight()
        except Exception as exc:
            self._log("preflight", 0, ok=False, error_type=type(exc).__name__)
            raise _Terminal("failed_infrastructure", f"preflight failed: {type(exc).__name__}") from exc
        self._log("preflight", 0, ok=True)

    def _execute(self):
        self._run_preflight()
        self._plan()
        for rnd in range(1, self.max_rounds + 1):
            self._rounds = rnd
            new = self._work(rnd)
            if not new:
                self._codes.append({"code": "no_new_candidate"})
                continue
            best = self._screen(rnd, new)
            if best is None:
                continue
            verdict_id = self._verify(rnd, best)
            if verdict_id is None:
                continue
            self._accept(rnd, best, verdict_id)
        raise _Terminal("rounds_exhausted", f"no acceptance after {self.max_rounds} rounds")

    # -- run ---------------------------------------------------------------------------
    def run(self):
        if self.ledger_path.exists() and self.ledger_path.stat().st_size:
            raise ValueError("ledger already exists; one run per official ledger")
        self._start = self._clock()
        self._deadline = self._start + self.deadline_seconds
        self._log("run_started", 0, session_id=self.session_id, deadline_seconds=self.deadline_seconds,
                  max_rounds=self.max_rounds, screen_bar=self.screen_bar, models={
                      key: {"primary": order.primary, "fallback": order.fallback}
                      for key, order in self.orders.items()},
                  budget=self.budget.snapshot())
        for item in self.annotations:
            self._log(item["type"], 0, **{k: v for k, v in item.items() if k != "type"})
        try:
            self._execute()  # always ends by raising _Terminal
        except _Terminal as stop:
            state, reason, attempts = stop.state, stop.reason, stop.attempts
        except BudgetExhausted as exc:
            state, reason, attempts = "failed_budget", str(exc)[:200], getattr(exc, "attempts", None)
        except Exception as exc:  # bounded termination beats a hung or half-logged run
            state, reason, attempts = ("failed_infrastructure",
                                       f"unexpected {type(exc).__name__}", None)
            try:  # diagnostics stay out of the ledger: tracebacks can echo model or harness text
                (self.ledger_path.parent / "diagnostics.log").write_text(traceback.format_exc())
            except OSError:
                pass
        fields = {"attempts": attempts} if attempts else {}
        sent = self._forwarded
        record = self._log(
            "terminal", self._rounds, state=state, reason=reason,
            last_forwarded_candidate_id=sent["candidate_id"] if sent else None,
            last_forwarded_candidate_sha256=sent["sha256"] if sent else None,
            budget=self.budget.snapshot(), **fields)
        self.playbook.record(self.events[-1])
        return RunResult(run_id=self.run_id, state=state, rounds=self._rounds,
                         best_response=self._best_response, ledger_path=str(self.ledger_path),
                         ledger_final_sha256=record["sha256"], budget=self.budget.snapshot(),
                         events=self.events)
