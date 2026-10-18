"""Parallel rounds: workers, screens and verifiers overlap; the ledger stays deterministic.

Everything is fake: scripted model clients, FakeHarness, stub backends. Barriers carry a timeout,
so a regression to sequential execution fails the test instead of hanging it.
"""

import json
import os
from pathlib import Path
import random
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_team import config as nfw_config, live
from agent_team.__main__ import PLAN, SOURCE_A, SOURCE_B
from agent_team.budget import Budget
from agent_team.contract import make_candidate
from agent_team.fake_harness import FakeHarness
from agent_team.faults import FaultInjectingClient
from agent_team.models import FixtureModelClient, ModelError, ModelReply
from agent_team.orchestrator import Orchestrator
from optimizer_core import harness
from optimizer_core.ledger import append_event, verify_ledger

from test_orchestration import FakeClock, ORDERS, fence, harness_script

SHA_A = make_candidate("x", SOURCE_A)["sha256"]
SHA_B = make_candidate("x", SOURCE_B)["sha256"]
SHA_RE = re.compile(r"Candidate sha256: ([0-9a-f]{64})")
WAIT = 5.0  # barrier timeout: long enough to be safe, short enough to fail a regression quickly


class RoleClient:
    """Thread-safe scripted client keyed by role, so concurrent callers never race for a queue.

    verdicts maps sha -> True | False | "mismatch" | Exception (default approve). barrier_roles
    make those roles rendezvous in complete(); delays add per-role sleeps (fake model latency)."""

    def __init__(self, *, verdicts=None, barrier_roles=(), barrier_timeout=WAIT, delays=None,
                 jitter=0.0, sources=None):
        self.verdicts = dict(verdicts or {})
        self.barrier_roles = set(barrier_roles)
        self.barrier = threading.Barrier(2)
        self.barrier_timeout = barrier_timeout
        self.delays, self.jitter = dict(delays or {}), jitter
        self.sources = sources or {"worker_a": SOURCE_A, "worker_b": SOURCE_B}
        self.calls, self._lock = [], threading.Lock()

    def complete(self, model_id, role, messages, max_tokens):
        with self._lock:
            self.calls.append((model_id, role))
        if role in self.barrier_roles:
            self.barrier.wait(self.barrier_timeout)
        if self.jitter:
            time.sleep(random.uniform(0, self.jitter))
        time.sleep(self.delays.get(role, 0))
        if role == "planner":
            return ModelReply(PLAN, 10)
        if role in self.sources:
            return ModelReply(fence(self.sources[role]), 10)
        sha = SHA_RE.search(messages[-1]["content"]).group(1)
        verdict = self.verdicts.get(sha, True)
        if isinstance(verdict, Exception):
            raise verdict
        reported = "0" * 64 if verdict == "mismatch" else sha
        return ModelReply(json.dumps({"approved": verdict is True, "candidate_sha256": reported,
                                      "rationale": "fixture"}), 10)

    def roles(self, role):
        return [r for _, r in self.calls if r == role]


class TimedHarness:
    """FakeHarness wrapper: optional screen barrier and fake latency per mode."""

    def __init__(self, script=None, *, barrier=False, delays=None, barrier_timeout=WAIT):
        self.inner = FakeHarness(script if script is not None else harness_script())
        self.barrier, self.barrier_timeout = threading.Barrier(2), barrier_timeout
        self.use_barrier, self.delays = barrier, dict(delays or {})
        self.threads, self._lock = [], threading.Lock()

    def __call__(self, request):
        mode = request["mode"]
        with self._lock:
            self.threads.append(threading.get_ident())
        if mode == "screen" and self.use_barrier:
            self.barrier.wait(self.barrier_timeout)
        time.sleep(self.delays.get(mode, 0))
        return self.inner.evaluate_candidate(request)

    @property
    def calls(self):
        return self.inner.calls


class ParallelCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.n = 0

    def orchestrate(self, client, evaluate=None, *, parallel=True, max_rounds=3, **kwargs):
        self.n += 1
        directory = self.root / f"run{self.n}"
        directory.mkdir()
        ids = iter(range(1, 10_000))
        self.harness = evaluate if evaluate is not None else TimedHarness()
        self.client = client
        orch = Orchestrator(
            evaluate=self.harness, client=client, budget=Budget(),
            ledger_path=directory / "ledger.jsonl", session_id="session-1", orders=ORDERS,
            deadline_seconds=900, clock=FakeClock(), utcnow=lambda: "2026-10-17T00:00:00Z",
            new_id=lambda: f"{next(ids):04d}", max_rounds=max_rounds, parallel=parallel, **kwargs)
        result = orch.run()
        verify_ledger(result.ledger_path)
        return result

    @staticmethod
    def shape(result, drop=lambda event: False):
        return [(e["type"], e.get("candidate_id"), e.get("worker"), e.get("phase"))
                for e in result.events if not drop(e)]

    @staticmethod
    def of(result, kind):
        return [e for e in result.events if e["type"] == kind]


class ConcurrencyTests(ParallelCase):
    def test_workers_really_run_concurrently(self):
        result = self.orchestrate(RoleClient(barrier_roles={"worker_a", "worker_b"}))
        self.assertEqual(result.state, "simulation_completed", result.events[-1])
        self.assertEqual([e["worker"] for e in self.of(result, "candidate_created")],
                         ["worker_a", "worker_b"])

    def test_sequential_workers_cannot_pass_the_barrier(self):
        # Proves the barrier test has teeth: the sequential path deadlocks the rendezvous.
        client = RoleClient(barrier_roles={"worker_a", "worker_b"}, barrier_timeout=0.2)
        result = self.orchestrate(client, parallel=False)
        self.assertEqual(result.state, "failed_infrastructure")

    def test_screens_run_concurrently(self):
        evaluate = TimedHarness(barrier=True)
        result = self.orchestrate(RoleClient(), evaluate)
        self.assertEqual(result.state, "simulation_completed", result.events[-1])
        screens = [c for c in evaluate.calls if c["mode"] == "screen"]
        self.assertEqual(sorted(c["candidate"]["candidate_id"] for c in screens),
                         ["worker_a-r1", "worker_b-r1"])
        self.assertEqual(len(set(evaluate.threads[:2])), 2)  # two different threads screened

    def test_sequential_screens_cannot_pass_the_barrier(self):
        result = self.orchestrate(RoleClient(), TimedHarness(barrier=True, barrier_timeout=0.2),
                                  parallel=False)
        self.assertEqual(result.state, "failed_infrastructure")
        self.assertEqual(self.of(result, "harness_error")[0]["error_type"], "BrokenBarrierError")

    def test_verifier_overlaps_the_screens(self):
        # The verifier rendezvous with a screen: both must be in flight at once.
        gate = threading.Barrier(3)
        base = TimedHarness()

        def evaluate(request):
            if request["mode"] == "screen" and request["candidate"]["candidate_id"] == "worker_a-r1":
                gate.wait(WAIT)
            return base(request)

        class GatedClient(RoleClient):
            def complete(self, model_id, role, messages, max_tokens):
                if role == "verifier" and SHA_A in messages[-1]["content"]:
                    gate.wait(WAIT)
                return super().complete(model_id, role, messages, max_tokens)

        # Three parties: A's screen, A's verifier, and B's screen (which only needs to be started).
        def evaluate_with_b(request):
            if request["mode"] == "screen" and request["candidate"]["candidate_id"] == "worker_b-r1":
                gate.wait(WAIT)
            return evaluate(request)

        result = self.orchestrate(GatedClient(), evaluate_with_b)
        self.assertEqual(result.state, "simulation_completed", result.events[-1])


class DeterminismTests(ParallelCase):
    def test_ledger_order_matches_sequential_and_is_repeatable(self):
        sequential = self.orchestrate(RoleClient(), parallel=False)
        chosen = lambda e: e["type"] == "verifier_verdict" and e["candidate_id"] != "worker_a-r1"
        want = self.shape(sequential)
        for _ in range(5):
            parallel = self.orchestrate(RoleClient(jitter=0.02), TimedHarness(delays={"screen": 0.01}))
            # Same events in the same order; parallel adds only the unchosen candidates' verdicts.
            self.assertEqual(self.shape(parallel, chosen), want)
            self.assertEqual(sequential.state, parallel.state)
            types = [e["type"] for e in parallel.events]
            self.assertEqual(types.count("verifier_verdict"), 2)
            screens = [i for i, t in enumerate(types) if t == "screen_result"]
            verdicts = [i for i, t in enumerate(types) if t == "verifier_verdict"]
            self.assertLess(max(screens), min(verdicts))  # verdicts logged after the screens
            self.assertEqual([e["candidate_id"] for e in self.of(parallel, "verifier_verdict")],
                             ["worker_a-r1", "worker_b-r1"])
            self.assertEqual([e["worker"] for e in self.of(parallel, "candidate_created")],
                             ["worker_a", "worker_b"])

    def test_default_orchestrator_is_sequential(self):
        client, evaluate = RoleClient(), TimedHarness()
        result = self.orchestrate(client, evaluate, parallel=False)
        self.assertEqual(len(client.roles("verifier")), 1)
        self.assertEqual(len(self.of(result, "verifier_verdict")), 1)
        self.assertEqual(result.state, "simulation_completed")
        orch = Orchestrator(evaluate=evaluate, client=client, budget=Budget(),
                            ledger_path=self.root / "x.jsonl", session_id="s", orders=ORDERS,
                            deadline_seconds=900)
        self.assertIs(orch.parallel, False)
        with self.assertRaises(ValueError):
            Orchestrator(evaluate=evaluate, client=client, budget=Budget(),
                         ledger_path=self.root / "y.jsonl", session_id="s", orders=ORDERS,
                         deadline_seconds=900, parallel="yes")


class VerifierSelectionTests(ParallelCase):
    def two_qualifying(self):
        return TimedHarness(harness_script(**{
            "worker_b-r1": {"screen": {"screen_median_speedup": 3.5},
                            "acceptance": {"acceptance": {"median_speedup": 3.6, "ci_lower": 3.3}}}}))

    def test_verifier_runs_for_both_candidates(self):
        client = RoleClient()
        result = self.orchestrate(client, self.two_qualifying())
        self.assertEqual(len(client.roles("verifier")), 2)
        verdicts = self.of(result, "verifier_verdict")
        self.assertEqual([v["approved"] for v in verdicts], [True, True])
        # The best candidate (A, 4.0) is accepted with its own minted verdict id.
        accept = [c for c in self.harness.calls if c["mode"] == "acceptance"]
        self.assertEqual(len(accept), 1)
        self.assertEqual(accept[0]["candidate"]["candidate_id"], "worker_a-r1")
        self.assertEqual(accept[0]["verifier"]["evidence_ids"], [verdicts[0]["verdict_id"]])
        self.assertNotEqual(verdicts[0]["verdict_id"], verdicts[1]["verdict_id"])

    def assert_second_chosen(self, result):
        accept = [c for c in self.harness.calls if c["mode"] == "acceptance"]
        self.assertEqual([c["candidate"]["candidate_id"] for c in accept], ["worker_b-r1"])
        a, b = self.of(result, "verifier_verdict")
        self.assertFalse(a["approved"])
        self.assertNotIn("verdict_id", a)  # ids are minted for approvals only
        self.assertTrue(b["approved"])
        self.assertEqual(accept[0]["verifier"], {"approved": True, "evidence_ids": [b["verdict_id"]]})
        self.assertEqual(result.events[-1]["last_forwarded_candidate_id"], "worker_b-r1")
        self.assertEqual(result.best_response["candidate_id"], "worker_b-r1")
        self.assertEqual(result.state, "simulation_completed")

    def test_rejected_top_candidate_falls_back_to_the_approved_second(self):
        result = self.orchestrate(RoleClient(verdicts={SHA_A: False}), self.two_qualifying())
        self.assert_second_chosen(result)
        self.assertEqual(result.rounds, 1)

    def test_sha_mismatch_counts_as_rejected(self):
        result = self.orchestrate(RoleClient(verdicts={SHA_A: "mismatch"}), self.two_qualifying())
        self.assert_second_chosen(result)
        self.assertIs(self.of(result, "verifier_verdict")[0]["mismatch"], True)

    def test_verifier_order_failure_counts_as_not_approved(self):
        result = self.orchestrate(RoleClient(verdicts={SHA_A: ModelError("down", tokens=0)}),
                                  self.two_qualifying())
        self.assert_second_chosen(result)
        failed = self.of(result, "verifier_verdict")[0]
        self.assertIs(failed["verifier_failed"], True)
        self.assertEqual(len(failed["attempts"]), 3)

    def test_unscreened_or_below_bar_candidates_are_never_chosen(self):
        # B is approved but below the bar: only A qualifies, and A is rejected.
        result = self.orchestrate(RoleClient(verdicts={SHA_A: False}), max_rounds=1,
                                  screen_bar=3.0)
        self.assertEqual(result.state, "rounds_exhausted")
        self.assertEqual([c for c in self.harness.calls if c["mode"] == "acceptance"], [])
        self.assertEqual([v["approved"] for v in self.of(result, "verifier_verdict")], [False, True])

    def test_all_rejected_continues_to_the_next_round(self):
        result = self.orchestrate(RoleClient(verdicts={SHA_A: False, SHA_B: False}),
                                  self.two_qualifying(), max_rounds=1)
        self.assertEqual(result.state, "rounds_exhausted")
        self.assertEqual(len(self.of(result, "verifier_verdict")), 2)
        self.assertEqual(result.events[-1]["last_forwarded_candidate_id"], "worker_b-r1")

    def test_ties_choose_the_first_candidate(self):
        both = TimedHarness(harness_script(**{
            "worker_b-r1": {"screen": {"screen_median_speedup": 4.0}}}))
        self.orchestrate(RoleClient(), both)
        accept = [c for c in self.harness.calls if c["mode"] == "acceptance"]
        self.assertEqual(accept[0]["candidate"]["candidate_id"], "worker_a-r1")


class ErrorOrderTests(ParallelCase):
    def test_first_worker_terminal_wins_and_nothing_is_logged_from_threads(self):
        class Both(RoleClient):
            def complete(self, model_id, role, messages, max_tokens):
                if role in ("worker_a", "worker_b"):
                    time.sleep(0.05 if role == "worker_a" else 0)  # b finishes first
                    raise KeyError(role)
                return super().complete(model_id, role, messages, max_tokens)
        result = self.orchestrate(Both(), max_rounds=1, **{})
        self.assertEqual(result.state, "failed_infrastructure")
        self.assertEqual(result.events[-1]["reason"], "unexpected KeyError")
        diagnostics = (Path(result.ledger_path).parent / "diagnostics.log").read_text()
        self.assertIn("KeyError: 'worker_a'", diagnostics)

    def test_one_failed_worker_is_logged_in_order_and_the_other_continues(self):
        class OneDown(RoleClient):
            def complete(self, model_id, role, messages, max_tokens):
                if role == "worker_a":
                    raise ModelError("down", tokens=0)
                return super().complete(model_id, role, messages, max_tokens)
        result = self.orchestrate(OneDown(), max_rounds=1)
        kinds = [(e["type"], e.get("worker")) for e in result.events
                 if e["type"] in ("worker_failed", "candidate_created")]
        self.assertEqual(kinds, [("worker_failed", "worker_a"), ("candidate_created", "worker_b")])

    def test_both_workers_failing_in_round_one_is_failed_no_candidates(self):
        class Down(RoleClient):
            def complete(self, model_id, role, messages, max_tokens):
                if role in ("worker_a", "worker_b"):
                    raise ModelError("down", tokens=0)
                return super().complete(model_id, role, messages, max_tokens)
        result = self.orchestrate(Down(), max_rounds=1)
        self.assertEqual(result.state, "failed_no_candidates")
        self.assertEqual([e["worker"] for e in self.of(result, "worker_failed")],
                         ["worker_a", "worker_b"])

    def test_screen_infrastructure_error_and_budget_exhausted_rules_still_apply(self):
        script = harness_script(**{
            "worker_a-r1": {"screen": {"status": "budget_exhausted", "correctness_passed": False,
                                       "screen_median_speedup": None}},
            "worker_b-r1": {"screen": {"screen_median_speedup": 3.5}}})
        result = self.orchestrate(RoleClient(), TimedHarness(script))
        self.assertEqual(result.state, "simulation_completed")  # B carried the round
        script["worker_b-r1"] = {"screen": {"status": "infrastructure_error",
                                            "correctness_passed": False,
                                            "screen_median_speedup": None}}
        result = self.orchestrate(RoleClient(), TimedHarness(script))
        self.assertEqual(result.state, "failed_infrastructure")
        self.assertEqual(len(self.of(result, "screen_result")), 2)  # both logged before terminal

    def test_harness_exception_in_a_screen_thread_is_logged_by_the_controller(self):
        script = harness_script(**{"worker_b-r1": RuntimeError("boom")})
        result = self.orchestrate(RoleClient(), TimedHarness(script))
        self.assertEqual(result.state, "failed_infrastructure")
        error = self.of(result, "harness_error")[0]
        self.assertEqual((error["candidate_id"], error["error_type"]), ("worker_b-r1", "RuntimeError"))
        self.assertEqual(len(self.of(result, "screen_result")), 1)  # A's result precedes B's error


class SpeedTests(ParallelCase):
    def test_parallel_round_is_faster_with_fake_latency(self):
        delays = {"worker_a": 0.15, "worker_b": 0.15, "verifier": 0.1}
        timings = {}
        for parallel in (False, True):
            started = time.monotonic()
            result = self.orchestrate(RoleClient(delays=delays),
                                      TimedHarness(delays={"screen": 0.2}), parallel=parallel)
            timings[parallel] = time.monotonic() - started
            self.assertEqual(result.state, "simulation_completed")
        self.assertLess(timings[True], timings[False] * 0.75, timings)


class ThreadSafetyTests(unittest.TestCase):
    def hammer(self, target, threads=8):
        errors = []

        def run(index):
            try:
                target(index)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        pool = [threading.Thread(target=run, args=(i,)) for i in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join(30)
        self.assertEqual(errors, [])

    def test_budget_has_no_lost_updates(self):
        budget = Budget(max_attempts=10**6, task_attempts=10**6, max_tokens=10**9,
                        max_cost_usd=10**6, usd_per_1k_tokens=2.0)

        def work(index):
            for _ in range(250):
                reservation = budget.reserve(10)
                budget.snapshot()
                budget.settle(reservation, 7)
                budget.check_limits()
        self.hammer(work)
        snap = budget.snapshot()
        self.assertEqual(snap["attempts_used"], 8 * 250)
        self.assertEqual(snap["tokens_used"], 8 * 250 * 7)
        self.assertEqual(snap["open_reservations"], 0)
        self.assertAlmostEqual(snap["cost_usd"], 8 * 250 * 7 * 2.0 / 1000)

    def test_budget_limits_hold_under_concurrency(self):
        budget = Budget(max_attempts=10**6, task_attempts=10**6, max_tokens=1000, max_cost_usd=10**6)
        granted = []

        def work(index):
            for _ in range(50):
                try:
                    budget.reserve(10)
                    granted.append(1)
                except Exception:
                    pass
        self.hammer(work)
        self.assertEqual(len(granted), 100)  # never over-grants the token cap
        self.assertEqual(budget.snapshot()["tokens_used"], 1000)

    def test_ledger_appends_from_threads_keep_the_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "ledger.jsonl"

            def work(index):
                for n in range(50):
                    append_event(path, {"type": "t", "thread": index, "n": n})
            self.hammer(work)
            records = verify_ledger(path)
            self.assertEqual(len(records), 400)
            seen = {(r["event"]["thread"], r["event"]["n"]) for r in records}
            self.assertEqual(len(seen), 400)

    def test_ledger_lock_is_per_resolved_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.jsonl"
            b = Path(tmp) / "sub" / ".." / "a.jsonl"
            from optimizer_core import ledger
            (Path(tmp) / "sub").mkdir()
            self.assertIs(ledger._lock_for(a), ledger._lock_for(b))
            self.assertIsNot(ledger._lock_for(a), ledger._lock_for(Path(tmp) / "other.jsonl"))

    def test_fixture_client_pops_each_item_exactly_once(self):
        items = [f"reply-{i}" for i in range(400)]
        client = FixtureModelClient({"m": items})
        got = []

        def work(index):
            for _ in range(50):
                got.append(client.complete("m", "r", [{"role": "user", "content": "x"}], 10).text)
        self.hammer(work)
        self.assertEqual(sorted(got), sorted(items))
        self.assertEqual(len(client.calls), 400)

    def test_fault_client_injects_exactly_the_configured_failures(self):
        client = FaultInjectingClient(FixtureModelClient({"m": [f"r{i}" for i in range(100)]}),
                                      faults={"m": 3})
        failures = []

        def work(index):
            for _ in range(10):
                try:
                    client.complete("m", "r", [{"role": "user", "content": "x"}], 10)
                except ModelError:
                    failures.append(1)
        self.hammer(work)
        self.assertEqual(len(failures), 3)
        self.assertEqual(client.injected_total, 3)
        self.assertEqual(sorted(e["call_index"] for e in client.log), list(range(1, 81)))

    def test_crusoe_client_keeps_no_per_call_state(self):
        from agent_team.crusoe import CrusoeClient
        body = json.dumps({"choices": [{"message": {"content": "ok"}}],
                           "usage": {"total_tokens": 5}}).encode()
        opened = []

        def opener(method, url, headers, payload, timeout):
            opened.append(threading.get_ident())
            return 200, body
        client = CrusoeClient(api_key="k", opener=opener)
        replies = []

        def work(index):
            replies.append(client.complete("m", "r", [{"role": "user", "content": "x"}], 10))
        self.hammer(work)
        self.assertEqual(len(replies), 8)
        self.assertEqual({r.text for r in replies}, {"ok"})
        self.assertEqual(vars(client).keys(), {"_api_key", "base_url", "timeout", "_opener", "_model_params"})


class HarnessSlotTests(unittest.TestCase):
    def run_callers(self, count, *, environ=None):
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        class StubHarness:
            def __init__(self, backend, **kwargs):
                pass

            def evaluate_candidate(self, request):
                with lock:
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                try:
                    if environ is None:
                        try:  # two evaluations must be in flight together
                            barrier.wait(0.5)
                        except threading.BrokenBarrierError:
                            pass
                    time.sleep(0.05)
                finally:
                    with lock:
                        state["active"] -= 1
                return {"ok": True}

        class Backend:
            def __enter__(self):
                return object()

            def __exit__(self, *exc):
                return False

        errors = []

        def call():
            try:
                harness.evaluate_candidate({"session_id": "s"})
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
        env = dict(os.environ)
        env.pop("NFW_MAX_PARALLEL_EVALS", None)
        env.update(environ or {})
        with mock.patch.object(harness, "_slots", None), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("optimizer_core.backend.verified_backend", lambda: Backend()), \
                mock.patch("optimizer_core.sessions.load_session",
                           lambda session_id: {"seed": "s", "workload_lines": 1}), \
                mock.patch.object(harness, "Harness", StubHarness):
            threads = [threading.Thread(target=call) for _ in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(errors, [])
        return state["peak"]

    def test_two_evaluations_run_at_once_and_a_third_waits(self):
        self.assertEqual(self.run_callers(4), 2)

    def test_env_override_to_one_serializes(self):
        self.assertEqual(self.run_callers(3, environ={"NFW_MAX_PARALLEL_EVALS": "1"}), 1)

    def test_env_override_to_four(self):
        self.assertEqual(self.run_callers(6, environ={"NFW_MAX_PARALLEL_EVALS": "4"}), 4)

    def test_slot_setting_is_validated(self):
        self.assertEqual(harness.max_parallel_evals({}), 2)
        for good in ("1", "2", "4", " 3 "):
            self.assertIn(harness.max_parallel_evals({"NFW_MAX_PARALLEL_EVALS": good}), (1, 2, 3, 4))
        for bad in ("0", "5", "-1", "two", "1.5"):
            with self.assertRaises(ValueError):
                harness.max_parallel_evals({"NFW_MAX_PARALLEL_EVALS": bad})


class ConfigTests(unittest.TestCase):
    def config(self, **run):
        value = nfw_config.load_config()
        value = json.loads(json.dumps({**value, "models": {k: list(v) for k, v in value["models"].items()}}))
        value["run"].update(run)
        return value

    def test_project_config_enables_parallel(self):
        self.assertIs(nfw_config.load_config()["run"]["parallel"], True)

    def test_parallel_is_validated_and_defaults_to_false_when_absent(self):
        for flag in (True, False):
            self.assertIs(nfw_config.validate_config(self.config(parallel=flag))["run"]["parallel"], flag)
        absent = self.config()
        del absent["run"]["parallel"]
        self.assertIs(nfw_config.validate_config(absent)["run"]["parallel"], False)
        for bad in ("true", 1, None, [True]):
            with self.assertRaises(nfw_config.ConfigError):
                nfw_config.validate_config(self.config(parallel=bad))

    def build(self, **kwargs):
        ids = ["nvidia/NVIDIA-Nemotron-3-Super-120B-A12B", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
               "zai-org/GLM-5.3-Flash", "deepseek-ai/Deepseek-V4-Flash"]
        context = ("repo-task", "SUMMARY", {"type": "run_label", "label": "x"},
                   {"task_id": "repo-task", "kind": "repo", "include": "full", "n_files": 1,
                    "bundle_bytes": 1})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(live, "make_preflight", lambda session_id: (lambda: None)), \
                mock.patch.object(live, "_task_context", return_value=context):
            orchestrator, _ = live.build_live_run(
                session_id="s", output_dir=Path(tmp) / "run", client=FixtureModelClient({}),
                available_ids=ids, evaluate=lambda request: None, **kwargs)
        return orchestrator

    def test_build_live_run_passes_the_config_value(self):
        self.assertIs(self.build().parallel, True)
        self.assertIs(self.build(parallel=False).parallel, False)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps(self.config(parallel=False)))
            with mock.patch.dict(os.environ, {nfw_config.ENV_VAR: str(path)}):
                self.assertIs(self.build().parallel, False)


if __name__ == "__main__":
    unittest.main()
