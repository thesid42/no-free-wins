import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_team.__main__ import PLAN, SOURCE_A, SOURCE_B, main
from agent_team.budget import Budget
from agent_team.calls import ModelOrder, call_with_fallback
from agent_team.contract import make_candidate
from agent_team.fake_harness import FakeHarness
from agent_team.models import FixtureModelClient, ModelError
from agent_team.orchestrator import DEFAULT_MIN_HARNESS_SECONDS, TERMINAL_STATES, Orchestrator
from optimizer_core.canonical import digest
from optimizer_core.ledger import verify_ledger

ROOT = Path(__file__).resolve().parents[1]
SHA_A = make_candidate("x", SOURCE_A)["sha256"]


def fence(source):
    return f"```python\n{source}```"


def variant(source, n):
    return f"{source}# variant {n}\n"


def verdict(sha, approved=True):
    return json.dumps({"approved": approved, "candidate_sha256": sha, "rationale": "fixture"})


def plan(*ids):
    return json.dumps({"strategies": [{"strategy_id": i, "description": f"strategy {i}"}
                                      for i in ids]})


ORDERS = {"planner": ModelOrder("p-primary", "p-fallback"),
          "worker_a": ModelOrder("a-primary", "a-fallback"),
          "worker_b": ModelOrder("b-primary", "b-fallback"),
          "verifier": ModelOrder("v-primary", "v-fallback")}


def script(**overrides):
    base = {"p-primary": [PLAN], "a-primary": [fence(SOURCE_A)], "b-primary": [fence(SOURCE_B)],
            "v-primary": [verdict(SHA_A)]}
    base.update(overrides)
    return base


def harness_script(**extra):
    base = {"worker_a-r1": {"screen": {"screen_median_speedup": 4.0},
                            "acceptance": {"acceptance": {"median_speedup": 3.6, "ci_lower": 3.3}}},
            "worker_b-r1": {"screen": {"screen_median_speedup": 2.0}}}
    base.update(extra)
    return base


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TickingClient:
    """Advances the fake clock on every model call (or only for the given roles)."""

    def __init__(self, inner, clock, step, roles=None):
        self.inner, self.clock, self.step, self.roles = inner, clock, step, roles

    @property
    def calls(self):
        return self.inner.calls

    def complete(self, model_id, role, messages, max_tokens):
        if self.roles is None or role in self.roles:
            self.clock.now += self.step
        return self.inner.complete(model_id, role, messages, max_tokens)


SMALL_FLOORS = {"screen": 1, "acceptance": 1}


def measured_metrics(**overrides):
    metrics = {"accepted": True, "origin": "measured", "pairs": 30, "median_speedup": 3.5,
               "ci_lower": 3.4, "ci_upper": 3.6, "reasons": [],
               "policy": {"pairs": 30, "minimum_speedup": 1.0, "bootstrap_resamples": 10_000,
                          "bootstrap_seed": 42, "ci_level": 0.95}}
    metrics.update(overrides)
    return metrics


def measured_acceptance(request, **overrides):
    resp = {"schema_version": 1, "request_id": request["request_id"],
            "candidate_id": request["candidate"]["candidate_id"], "status": "accepted",
            "origin": "measured", "accepted": True, "correctness_passed": True,
            "screen_median_speedup": None,
            "acceptance": measured_metrics(),
            "receipt_id": "receipt-1", "receipt_sha256": "ab" * 32,
            "evidence_ids": ["receipt-evidence-1"], "feedback_public": []}
    resp.update(overrides)
    return resp


class OrchestrationCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.ids = iter(range(1, 10_000))

    def orchestrate(self, client_script=None, harness=None, evaluate=None, client=None,
                    clock=None, allow_unexpected=False, **kwargs):
        self.client = client or FixtureModelClient(client_script or script())
        self.harness = harness or FakeHarness(harness_script())
        params = dict(evaluate=evaluate or self.harness.evaluate_candidate, client=self.client,
                      budget=Budget(), ledger_path=self.dir / "ledger.jsonl",
                      session_id="session-1", orders=ORDERS, deadline_seconds=900,
                      clock=clock or FakeClock(), utcnow=lambda: "2026-10-17T00:00:00Z",
                      new_id=lambda: f"{next(self.ids):04d}")
        params.update(kwargs)
        self.orch = Orchestrator(**params)
        self.result = self.orch.run()
        if not allow_unexpected:  # a catch-all termination is a bug unless the test provokes it
            reason = self.of("terminal")[0]["reason"]
            self.assertFalse(reason.startswith("unexpected "), reason)
        return self.result

    def types(self):
        return [event["type"] for event in self.result.events]

    def of(self, kind):
        return [event for event in self.result.events if event["type"] == kind]

    def modes(self, mode):
        return [call for call in self.harness.calls if call["mode"] == mode]


class HappyAndFailureTests(OrchestrationCase):
    def test_happy_path_simulation_completed(self):
        result = self.orchestrate()
        self.assertEqual(result.state, "simulation_completed")
        self.assertIn(result.state, TERMINAL_STATES)
        self.assertEqual(result.rounds, 1)
        records = verify_ledger(result.ledger_path)
        self.assertEqual(records[-1]["sha256"], result.ledger_final_sha256)
        self.assertEqual(records[-1]["event"]["type"], "terminal")
        self.assertEqual([r["event"] for r in records], result.events)
        types = self.types()
        for kind in ("run_started", "plan", "screen_result", "verifier_verdict",
                     "acceptance_result", "heartbeat", "terminal"):
            self.assertIn(kind, types)
        self.assertEqual(types.count("worker_order"), 2)
        self.assertEqual(types.count("terminal"), 1)
        terminal = self.of("terminal")[0]
        self.assertEqual(terminal["last_forwarded_candidate_id"], "worker_a-r1")
        self.assertEqual(terminal["last_forwarded_candidate_sha256"], SHA_A)
        self.assertNotIn("best_candidate_id", terminal)
        self.assertEqual(len(self.modes("screen")), 2)
        accept = self.modes("acceptance")
        self.assertEqual(len(accept), 1)
        self.assertEqual(accept[0]["candidate"]["candidate_id"], "worker_a-r1")
        verdict_event = self.of("verifier_verdict")[0]
        self.assertTrue(verdict_event["approved"])
        self.assertEqual(accept[0]["verifier"]["evidence_ids"], [verdict_event["verdict_id"]])
        self.assertEqual(verdict_event["candidate_sha256"], SHA_A)
        done = self.of("acceptance_result")[0]
        self.assertEqual(done["origin"], "synthetic")
        self.assertTrue(done["receipt_id"] and done["receipt_sha256"])  # recorded, but synthetic
        self.assertEqual(done["acceptance"]["origin"], "synthetic")
        self.assertEqual(done["acceptance"]["reasons"], ["synthetic_evidence"])
        self.assertEqual(result.best_response["candidate_id"], "worker_a-r1")

    def test_ledger_has_no_source_or_messages(self):
        self.orchestrate()
        text = (self.dir / "ledger.jsonl").read_text()
        self.assertNotIn("def solve", text)
        self.assertNotIn("Task logs-parse-v1", text)
        saved = (self.dir / "candidates" / "worker_a-r1.py").read_bytes()
        self.assertEqual(saved, SOURCE_A.encode())

    def test_crlf_source_preserved_exactly(self):
        crlf = "def solve(lines):\r\n    return {}\r\n"
        self.orchestrate(script(**{"a-primary": [fence(crlf)]}),
                         harness=FakeHarness({"worker_a-r1": {"screen": {"screen_median_speedup": 4.0}}}))
        saved = (self.dir / "candidates" / "worker_a-r1.py").read_bytes()
        self.assertEqual(saved, crlf.encode())
        sent = self.harness.calls[0]["candidate"]
        self.assertEqual(sent["sha256"], digest(saved))

    def test_fallback_recovery_after_injected_failures(self):
        client_script = script(**{"a-primary": [ModelError("injected fault: one"),
                                                ModelError("injected fault: two")],
                                  "a-fallback": [fence(SOURCE_A)]})
        result = self.orchestrate(client_script)
        self.assertEqual(result.state, "simulation_completed")
        created = [e for e in self.of("candidate_created") if e["worker"] == "worker_a"][0]
        self.assertEqual([a["kind"] for a in created["attempts"]], ["primary", "retry", "fallback"])
        self.assertEqual([a["outcome"] for a in created["attempts"]],
                         ["model_error", "model_error", "ok"])

    def test_persistent_planner_failure(self):
        result = self.orchestrate(script(**{"p-primary": [], "p-fallback": []}))
        self.assertEqual(result.state, "failed_plan")
        self.assertEqual(self.harness.calls, [])
        terminal = self.of("terminal")[0]
        self.assertEqual(len(terminal["attempts"]), 3)
        verify_ledger(result.ledger_path)

    def test_deadline_stops_model_calls(self):
        clock = FakeClock()
        client = TickingClient(FixtureModelClient(script()), clock, 400)
        result = self.orchestrate(client=client, clock=clock, deadline_seconds=500)
        self.assertEqual(result.state, "failed_deadline")
        self.assertEqual(self.harness.calls, [])
        self.assertEqual(self.types().count("terminal"), 1)

    def test_deadline_stops_second_screen_call(self):
        clock, calls = FakeClock(), []
        harness = FakeHarness(harness_script())

        def evaluate(request):
            calls.append(request["mode"])
            response = harness.evaluate_candidate(request)
            clock.now += 600  # the first screen runs past the 500s deadline
            return response

        result = self.orchestrate(client=TickingClient(FixtureModelClient(script()), clock, 1),
                                  clock=clock, deadline_seconds=500, harness=harness,
                                  evaluate=evaluate, min_harness_seconds=SMALL_FLOORS)
        self.assertEqual(result.state, "failed_deadline")
        self.assertEqual(calls, ["screen"])
        self.assertEqual(len(harness.calls), 1)
        self.assertEqual(self.of("verifier_verdict"), [])
        self.assertEqual(self.of("terminal")[0]["reason"], "deadline reached before next call")

    def test_deadline_stops_acceptance_call(self):
        clock = FakeClock()
        client = TickingClient(FixtureModelClient(script()), clock, 600, roles={"verifier"})
        result = self.orchestrate(client=client, clock=clock, deadline_seconds=500,
                                  min_harness_seconds=SMALL_FLOORS)
        self.assertEqual(result.state, "failed_deadline")
        self.assertEqual([c["mode"] for c in self.harness.calls], ["screen", "screen"])
        self.assertEqual(self.of("verifier_verdict"), [])  # late replies cannot become verdicts
        self.assertEqual(self.of("acceptance_result"), [])

    def test_time_floor_blocks_screen_call(self):
        result = self.orchestrate(deadline_seconds=900,
                                  min_harness_seconds={"screen": 1000, "acceptance": 1})
        self.assertEqual(result.state, "failed_deadline")
        self.assertEqual(self.of("terminal")[0]["reason"], "insufficient time for screen")
        self.assertEqual(self.harness.calls, [])

    def test_time_floor_blocks_acceptance_call(self):
        clock = FakeClock()
        client = TickingClient(FixtureModelClient(script()), clock, 50)
        result = self.orchestrate(client=client, clock=clock, deadline_seconds=1000,
                                  min_harness_seconds={"screen": 1, "acceptance": 900})
        self.assertEqual(result.state, "failed_deadline")  # 1000 - 4 model calls * 50 = 800 < 900
        self.assertEqual(self.of("terminal")[0]["reason"], "insufficient time for acceptance")
        self.assertEqual([c["mode"] for c in self.harness.calls], ["screen", "screen"])

    def test_default_floors_match_real_harness_bounds(self):
        self.assertEqual(DEFAULT_MIN_HARNESS_SECONDS, {"screen": 240, "acceptance": 840})
        self.orchestrate(deadline_seconds=300)  # acceptance floor 840 cannot fit in 300s
        self.assertEqual(self.result.state, "failed_deadline")

    def test_bad_floors_rejected(self):
        for bad in ({"screen": 1}, {"screen": -1, "acceptance": 1}, {"screen": 1, "acceptance": "x"}):
            with self.assertRaises(ValueError):
                self.orchestrate(min_harness_seconds=bad)
            (self.dir / "ledger.jsonl").unlink(missing_ok=True)

    def test_deadline_checked_before_retry_and_fallback(self):
        clock = FakeClock()
        fixture = FixtureModelClient(script(**{"p-primary": [ModelError("slow"), ModelError("slow")],
                                               "p-fallback": [PLAN]}))
        # the failed primary call pushes the clock past the 500s deadline
        result = self.orchestrate(client=TickingClient(fixture, clock, 600), clock=clock,
                                  deadline_seconds=500)
        self.assertEqual(result.state, "failed_deadline")
        self.assertEqual([c[0] for c in fixture.calls], ["p-primary"])
        self.assertEqual(self.harness.calls, [])
        attempts = self.of("terminal")[0]["attempts"]
        self.assertEqual([(a["kind"], a["outcome"]) for a in attempts], [("primary", "model_error")])

    def test_remaining_wall_seconds_tracks_clock(self):
        clock = FakeClock()
        client = TickingClient(FixtureModelClient(script()), clock, 10)
        self.orchestrate(client=client, clock=clock, deadline_seconds=900)
        self.assertEqual(self.harness.calls[0]["remaining_wall_seconds"], 900 - 30)

    def test_deadline_is_capped(self):
        self.orchestrate(deadline_seconds=99999)
        self.assertEqual(self.of("run_started")[0]["deadline_seconds"], 1800)
        self.assertEqual(self.harness.calls[0]["remaining_wall_seconds"], 1800)

    def test_budget_exhaustion(self):
        result = self.orchestrate(budget=Budget(max_attempts=2))
        self.assertEqual(result.state, "failed_budget")
        self.assertEqual(self.harness.calls, [])
        self.assertEqual(self.of("terminal")[0]["attempts"][-1]["outcome"], "budget_exhausted")

    def test_rejects_bad_orders_and_reused_ledger(self):
        with self.assertRaises(ValueError):
            self.orchestrate(orders={"planner": ORDERS["planner"]})
        self.orchestrate()
        with self.assertRaises(ValueError):
            self.orchestrate()


class CandidateAndPlanTests(OrchestrationCase):
    def test_missing_candidate_continues_with_other_worker(self):
        result = self.orchestrate(script(**{"b-primary": ["no code", "no code"],
                                            "b-fallback": ["still no code"]}))
        self.assertEqual(result.state, "simulation_completed")
        failed = self.of("worker_failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["worker"], "worker_b")
        self.assertEqual([a["outcome"] for a in failed[0]["attempts"]], ["malformed"] * 3)
        self.assertEqual(len(self.modes("screen")), 1)

    def test_both_workers_fail_in_round_one(self):
        result = self.orchestrate(script(**{"a-primary": ["x", "x"], "a-fallback": ["x"],
                                            "b-primary": ["x", "x"], "b-fallback": ["x"]}))
        self.assertEqual(result.state, "failed_no_candidates")
        self.assertEqual(self.harness.calls, [])

    def test_duplicate_candidate_screened_once(self):
        result = self.orchestrate(script(**{"b-primary": [fence(SOURCE_A)]}))
        self.assertEqual(result.state, "simulation_completed")
        self.assertEqual(len(self.of("duplicate_candidate")), 1)
        self.assertEqual(len(self.modes("screen")), 1)

    def test_single_strategy_fails(self):
        result = self.orchestrate(script(**{"p-primary": [plan("only-one")]}))
        self.assertEqual(result.state, "failed_missing_strategy")
        self.assertEqual(self.harness.calls, [])
        self.assertEqual(self.of("worker_order"), [])

    def test_same_strategy_id_fails(self):
        result = self.orchestrate(script(**{"p-primary": [plan("same", "same")]}))
        self.assertEqual(result.state, "failed_missing_strategy")

    def test_plan_and_handoff_events(self):
        self.orchestrate()
        plan_event = self.of("plan")[0]
        self.assertEqual([x["strategy_id"] for x in plan_event["strategies"]],
                         ["single-pass-regex", "split-and-dict"])
        orders = self.of("worker_order")
        self.assertEqual([(o["worker"], o["strategy_id"]) for o in orders],
                         [("worker_a", "single-pass-regex"), ("worker_b", "split-and-dict")])

    def test_plan_text_stays_out_of_ledger(self):
        self.orchestrate()
        plan_bytes = (self.dir / "plan.json").read_bytes()
        plan_event = self.of("plan")[0]
        self.assertEqual(plan_event["plan_sha256"], digest(plan_bytes))
        saved = json.loads(plan_bytes)
        self.assertEqual([digest(x["description"].encode()) for x in saved],
                         [x["description_sha256"] for x in plan_event["strategies"]])
        self.assertEqual([o["description_sha256"] for o in self.of("worker_order")],
                         [x["description_sha256"] for x in plan_event["strategies"]])
        ledger = (self.dir / "ledger.jsonl").read_text()
        for item in saved:
            self.assertIn(item["description"], plan_bytes.decode())
            self.assertNotIn(item["description"], ledger)

    def test_long_strategy_id_truncated_in_ledger(self):
        long_id = "x" * 100
        self.orchestrate(script(**{"p-primary": [plan(long_id, "other")]}))
        self.assertEqual(self.of("plan")[0]["strategies"][0]["strategy_id"], "x" * 64)
        self.assertEqual(self.of("worker_order")[0]["strategy_id"], "x" * 64)
        self.assertNotIn(long_id, (self.dir / "ledger.jsonl").read_text())
        self.assertIn(long_id, (self.dir / "plan.json").read_text())

    def test_missing_strategy_still_records_plan_hash(self):
        self.orchestrate(script(**{"p-primary": [plan("only-one")]}))
        self.assertEqual(self.of("plan")[0]["plan_sha256"],
                         digest((self.dir / "plan.json").read_bytes()))

    def test_best_candidate_is_fastest_screen(self):
        sha_b = make_candidate("x", SOURCE_B)["sha256"]
        harness = FakeHarness(harness_script(**{
            "worker_a-r1": {"screen": {"screen_median_speedup": 3.5}},
            "worker_b-r1": {"screen": {"screen_median_speedup": 5.0}}}))
        result = self.orchestrate(script(**{"v-primary": [verdict(sha_b)]}), harness=harness)
        self.assertEqual(result.state, "simulation_completed")
        self.assertEqual(self.of("verifier_verdict")[0]["candidate_id"], "worker_b-r1")
        self.assertEqual(self.modes("acceptance")[0]["candidate"]["candidate_id"], "worker_b-r1")

    def test_screen_tie_goes_to_first_worker(self):
        harness = FakeHarness(harness_script(**{
            "worker_a-r1": {"screen": {"screen_median_speedup": 4.0}},
            "worker_b-r1": {"screen": {"screen_median_speedup": 4.0}}}))
        self.orchestrate(harness=harness)
        self.assertEqual(self.of("verifier_verdict")[0]["candidate_id"], "worker_a-r1")
        self.assertEqual(self.modes("acceptance")[0]["candidate"]["candidate_id"], "worker_a-r1")

    def test_cross_round_duplicate_is_skipped(self):
        b2 = variant(SOURCE_B, 2)
        client_script = script(**{
            "a-primary": [fence(SOURCE_A), fence(SOURCE_A)],
            "b-primary": [fence(SOURCE_B), fence(b2)],
            "v-primary": [verdict(SHA_A, approved=False),
                          verdict(make_candidate("x", b2)["sha256"])]})
        harness = FakeHarness(harness_script(**{"worker_b-r2": {
            "screen": {"screen_median_speedup": 4.0}}}))
        result = self.orchestrate(client_script, harness=harness)
        self.assertEqual(result.state, "simulation_completed")
        self.assertEqual(result.rounds, 2)
        duplicates = self.of("duplicate_candidate")
        self.assertEqual(len(duplicates), 1)
        self.assertEqual((duplicates[0]["round"], duplicates[0]["worker"], duplicates[0]["duplicate_of"]),
                         (2, "worker_a", "worker_a-r1"))
        screened = [c["candidate"]["candidate_id"] for c in self.modes("screen")]
        self.assertEqual(screened, ["worker_a-r1", "worker_b-r1", "worker_b-r2"])


class EvidenceTests(OrchestrationCase):
    def evaluator(self, **overrides):
        fake = FakeHarness(harness_script())
        calls = []

        def evaluate(request):
            if request["mode"] == "screen":
                return fake.evaluate_candidate(request)
            calls.append(request)
            return measured_acceptance(request, **overrides)

        return fake, evaluate, calls

    def test_missing_receipt_is_failed_evidence(self):
        fake, evaluate, _ = self.evaluator(receipt_sha256=None)
        result = self.orchestrate(harness=fake, evaluate=evaluate)
        self.assertEqual(result.state, "failed_evidence")

    def test_synthetic_origin_accepted_is_failed_evidence(self):
        fake, evaluate, _ = self.evaluator(origin="synthetic")
        result = self.orchestrate(harness=fake, evaluate=evaluate)
        self.assertEqual(result.state, "failed_evidence")

    def test_short_pairs_is_failed_evidence(self):
        fake, evaluate, _ = self.evaluator(acceptance=measured_metrics(pairs=5))
        self.assertEqual(self.orchestrate(harness=fake, evaluate=evaluate).state, "failed_evidence")

    def test_nonempty_acceptance_reasons_is_failed_evidence(self):
        fake, evaluate, _ = self.evaluator(acceptance=measured_metrics(reasons=["evidence_incomplete"]))
        self.assertEqual(self.orchestrate(harness=fake, evaluate=evaluate).state, "failed_evidence")

    def test_synthetic_acceptance_origin_is_failed_evidence(self):
        fake, evaluate, _ = self.evaluator(acceptance=measured_metrics(origin="synthetic"))
        self.assertEqual(self.orchestrate(harness=fake, evaluate=evaluate).state, "failed_evidence")

    def test_verified_done_requires_full_measured_evidence(self):
        fake, evaluate, calls = self.evaluator()
        result = self.orchestrate(harness=fake, evaluate=evaluate)
        self.assertEqual(result.state, "verified_done")
        self.assertEqual(len(calls), 1)
        done = self.of("acceptance_result")[0]
        self.assertEqual((done["origin"], done["receipt_id"], done["receipt_sha256"]),
                         ("measured", "receipt-1", "ab" * 32))
        self.assertEqual(done["acceptance"]["pairs"], 30)

    def test_malformed_response_is_failed_evidence(self):
        fake = FakeHarness(harness_script())
        result = self.orchestrate(harness=fake, evaluate=lambda request: {"status": "screened"})
        self.assertEqual(result.state, "failed_evidence")
        self.assertEqual(self.of("evidence_invalid")[0]["mode"], "screen")

    def test_harness_exception_is_infrastructure_failure(self):
        def boom(request):
            raise RuntimeError("daytona exploded with SECRET-DETAIL")

        result = self.orchestrate(evaluate=boom)
        self.assertEqual(result.state, "failed_infrastructure")
        self.assertEqual(self.of("terminal")[0]["reason"], "harness raised in screen")
        error = self.of("harness_error")[0]
        self.assertEqual((error["mode"], error["error_type"]), ("screen", "RuntimeError"))
        self.assertNotIn("SECRET-DETAIL", (self.dir / "ledger.jsonl").read_text())

    def test_acceptance_harness_exception_is_infrastructure_failure(self):
        fake = FakeHarness(harness_script())

        def evaluate(request):
            if request["mode"] == "acceptance":
                raise RuntimeError("boom")
            return fake.evaluate_candidate(request)

        result = self.orchestrate(harness=fake, evaluate=evaluate)
        self.assertEqual(result.state, "failed_infrastructure")
        self.assertEqual(self.of("terminal")[0]["reason"], "harness raised in acceptance")
        self.assertEqual(self.of("harness_error")[0]["mode"], "acceptance")

    def test_infrastructure_status_responses(self):
        harness = FakeHarness(harness_script(**{"worker_a-r1": {
            "screen": {"status": "infrastructure_error", "screen_median_speedup": None}}}))
        self.assertEqual(self.orchestrate(harness=harness).state, "failed_infrastructure")
        self.assertEqual(self.of("terminal")[0]["reason"], "screen status infrastructure_error")
        self.assertEqual(self.of("harness_error"), [])

    def test_acceptance_infrastructure_status(self):
        fake, evaluate, _ = self.evaluator(status="infrastructure_error", accepted=False)
        self.assertEqual(self.orchestrate(harness=fake, evaluate=evaluate).state,
                         "failed_infrastructure")
        self.assertEqual(self.of("terminal")[0]["reason"], "acceptance status infrastructure_error")

    def test_harness_budget_exhausted_is_failed_deadline(self):
        fake, evaluate, _ = self.evaluator(status="budget_exhausted", accepted=False)
        self.assertEqual(self.orchestrate(harness=fake, evaluate=evaluate).state, "failed_deadline")
        self.assertEqual(self.of("terminal")[0]["reason"], "harness budget exhausted in acceptance")

    def exhausted_screen_a(self):
        return {"worker_a-r1": {"screen": {"status": "budget_exhausted",
                                           "screen_median_speedup": None}},
                "worker_a-r2": {"screen": {"screen_median_speedup": 4.0},
                                "acceptance": {"acceptance": {"median_speedup": 3.6,
                                                              "ci_lower": 3.3}}}}

    def test_screen_budget_exhausted_with_time_left_fails_only_that_candidate(self):
        a2, b2 = variant(SOURCE_A, 2), variant(SOURCE_B, 2)
        client_script = script(**{
            "a-primary": [fence(SOURCE_A), fence(a2)], "b-primary": [fence(SOURCE_B), fence(b2)],
            "v-primary": [verdict(make_candidate("x", a2)["sha256"])]})
        harness = FakeHarness(harness_script(**self.exhausted_screen_a()))
        result = self.orchestrate(client_script, harness=harness, screen_bar=3.0)
        self.assertEqual(result.state, "simulation_completed")
        self.assertEqual(result.rounds, 2)
        first, second = self.modes("screen")[:2]  # the other candidate was still screened
        self.assertEqual([first["candidate"]["candidate_id"], second["candidate"]["candidate_id"]],
                         ["worker_a-r1", "worker_b-r1"])
        screens = self.of("screen_result")
        self.assertEqual(screens[0]["status"], "budget_exhausted")
        self.assertEqual(screens[1]["status"], "screened")
        round_two = [c for c in self.client.calls if c[1] == "worker_a"][1][2]
        self.assertIn("evaluation_deadline", json.dumps(round_two))
        self.assertIn("screen_below_bar", json.dumps(round_two))  # worker_b really was below the bar

    def test_every_screen_exhausted_does_not_blame_the_bar(self):
        slow = {"screen": {"status": "budget_exhausted", "screen_median_speedup": None}}
        harness = FakeHarness({f"{w}-r1": slow for w in ("worker_a", "worker_b")})
        client_script = script(**{"a-primary": [fence(SOURCE_A), fence(variant(SOURCE_A, 2))],
                                  "b-primary": [fence(SOURCE_B), fence(variant(SOURCE_B, 2))]})
        self.orchestrate(client_script, harness=harness, max_rounds=2)
        round_two = [c for c in self.client.calls if c[1] == "worker_a"][1][2]
        self.assertIn("evaluation_deadline", json.dumps(round_two))
        self.assertNotIn("screen_below_bar", json.dumps(round_two))

    def test_screen_budget_exhausted_with_too_little_time_is_failed_deadline(self):
        clock = FakeClock()
        harness = FakeHarness(harness_script(**self.exhausted_screen_a()))

        def evaluate(request):
            response = harness.evaluate_candidate(request)
            if response["status"] == "budget_exhausted":
                clock.now += 850  # 50 s left, below the screen floor of 100 s
            return response
        result = self.orchestrate(evaluate=evaluate, clock=clock, harness=harness,
                                  min_harness_seconds={"screen": 100, "acceptance": 1})
        self.assertEqual(result.state, "failed_deadline")
        self.assertEqual(self.of("terminal")[0]["reason"], "harness budget exhausted in screen")
        self.assertEqual(len(self.modes("screen")), 1)

    def test_simulated_acceptance_with_measured_origin_is_failed_evidence(self):
        fake, evaluate, _ = self.evaluator(status="simulated_acceptance", accepted=False)
        self.assertEqual(self.orchestrate(harness=fake, evaluate=evaluate).state, "failed_evidence")

    def test_rejected_or_bar_not_met_with_accepted_true_is_failed_evidence(self):
        for status in ("rejected", "bar_not_met"):
            with self.subTest(status=status):
                (self.dir / "ledger.jsonl").unlink(missing_ok=True)
                self.ids = iter(range(1, 10_000))
                fake, evaluate, _ = self.evaluator(status=status, accepted=True)
                self.assertEqual(self.orchestrate(harness=fake, evaluate=evaluate).state,
                                 "failed_evidence")

    def test_bar_not_met_continues_to_next_round(self):
        fake = FakeHarness(harness_script())
        responses = []

        def evaluate(request):
            if request["mode"] == "acceptance":
                resp = measured_acceptance(
                    request, status="bar_not_met", accepted=False, receipt_id=None,
                    receipt_sha256=None,
                    feedback_public=[{"code": "below_bar", "summary": "median 2.1x",
                                      "evidence_ids": ["ev-9"]}])
                responses.append(resp)
                return resp
            return fake.evaluate_candidate(request)

        client_script = script(**{
            "a-primary": [fence(SOURCE_A), fence(variant(SOURCE_A, 2))],
            "b-primary": [fence(SOURCE_B), fence(variant(SOURCE_B, 2))],
            "v-primary": [verdict(SHA_A),
                          verdict(make_candidate("x", variant(SOURCE_A, 2))["sha256"])]})
        fake.script["worker_a-r2"] = {"screen": {"screen_median_speedup": 4.0}}
        secret = "SECRET-SESSION-xyz"
        result = self.orchestrate(client_script, harness=fake, evaluate=evaluate, max_rounds=2,
                                  session_id=secret)
        self.assertEqual(result.state, "rounds_exhausted")
        self.assertEqual(result.rounds, 2)
        self.assertEqual(len(responses), 2)  # one acceptance per round
        self.assertEqual(responses[0]["acceptance"]["policy"]["bootstrap_seed"], 42)
        round_two = [c for c in self.client.calls if c[1] in ("worker_a", "worker_b")][2:]
        self.assertEqual(len(round_two), 2)
        for _, _, messages in round_two:
            text = json.dumps(messages)
            self.assertIn("below_bar", text)
            self.assertIn("ev-9", text)
            self.assertNotIn("seed", text.lower())
            self.assertNotIn(secret, text)


class MultiRoundTests(OrchestrationCase):
    def two_round_script(self, first_verdict):
        a2, b2 = variant(SOURCE_A, 2), variant(SOURCE_B, 2)
        return script(**{
            "a-primary": [fence(SOURCE_A), fence(a2)], "b-primary": [fence(SOURCE_B), fence(b2)],
            "v-primary": [first_verdict, verdict(make_candidate("x", a2)["sha256"])]}), a2

    def run_two_rounds(self, first_verdict, **kwargs):
        client_script, _ = self.two_round_script(first_verdict)
        extra = {"worker_a-r2": {"screen": {"screen_median_speedup": 4.0},
                                 "acceptance": {"acceptance": {"median_speedup": 3.6, "ci_lower": 3.3}}}}
        return self.orchestrate(client_script, harness=FakeHarness(harness_script(**extra)), **kwargs)

    def assert_round_two_feedback(self, code):
        calls = [c for c in self.client.calls if c[1] in ("worker_a", "worker_b")]
        round_one, round_two = calls[:2], calls[2:]
        for call in round_one:
            self.assertNotIn(code, json.dumps(call[2]))
        self.assertEqual(len(round_two), 2)
        for call in round_two:
            self.assertIn(code, json.dumps(call[2]))

    def test_verifier_rejection_then_approval(self):
        result = self.run_two_rounds(verdict(SHA_A, approved=False))
        self.assertEqual(result.state, "simulation_completed")
        self.assertEqual(result.rounds, 2)
        self.assertEqual([e["approved"] for e in self.of("verifier_verdict")], [False, True])
        self.assert_round_two_feedback("verifier_rejected")
        self.assertEqual(self.modes("acceptance")[0]["candidate"]["candidate_id"], "worker_a-r2")

    def test_verifier_sha_mismatch_is_rejection(self):
        result = self.run_two_rounds(verdict("0" * 64, approved=True))
        self.assertEqual(result.state, "simulation_completed")
        first = self.of("verifier_verdict")[0]
        self.assertFalse(first["approved"])
        self.assertTrue(first["mismatch"])
        self.assertNotIn("verdict_id", first)
        self.assertEqual(result.rounds, 2)

    def test_verifier_order_failure_is_rejection(self):
        client_script, _ = self.two_round_script("not json")
        client_script["v-primary"] = ["not json", "not json", ] + client_script["v-primary"][1:]
        client_script["v-fallback"] = ["not json"]
        extra = {"worker_a-r2": {"screen": {"screen_median_speedup": 4.0}}}
        result = self.orchestrate(client_script, harness=FakeHarness(harness_script(**extra)))
        self.assertEqual(result.state, "simulation_completed")
        first = self.of("verifier_verdict")[0]
        self.assertTrue(first["verifier_failed"])
        self.assertEqual(len(first["attempts"]), 3)

    def test_screen_below_bar_every_round(self):
        a, b = SOURCE_A, SOURCE_B
        client_script = script(**{
            "a-primary": [fence(a), fence(variant(a, 2)), fence(variant(a, 3))],
            "b-primary": [fence(b), fence(variant(b, 2)), fence(variant(b, 3))]})
        slow = {"screen": {"screen_median_speedup": 2.0}}
        harness = FakeHarness({f"{w}-r{n}": slow for w in ("worker_a", "worker_b")
                               for n in (1, 2, 3)})
        secret = "SECRET-SESSION-xyz"
        result = self.orchestrate(client_script, harness=harness, max_rounds=3, session_id=secret, screen_bar=3.0)
        for _, _, messages in self.client.calls:
            self.assertNotIn(secret, json.dumps(messages))
        self.assertEqual(result.state, "rounds_exhausted")
        self.assertEqual(result.rounds, 3)
        self.assertEqual(self.modes("acceptance"), [])
        self.assertEqual(len(self.modes("screen")), 6)
        self.assertEqual(self.of("verifier_verdict"), [])
        self.assertIsNone(self.of("terminal")[0]["last_forwarded_candidate_id"])
        third = [c for c in self.client.calls if c[1] == "worker_a"][2][2]
        self.assertIn("screen_below_bar", json.dumps(third))


REPO_TASK_ID = "repo-abc123def456"
REPO_SUMMARY = "REPO-SUMMARY-MARKER: optimize pkg.mod.solve(data)"


class RepoTaskTests(OrchestrationCase):
    def test_repo_task_id_reaches_every_request_and_summary_every_prompt(self):
        harness = FakeHarness(harness_script(), task_id=REPO_TASK_ID)
        result = self.orchestrate(harness=harness, task_id=REPO_TASK_ID, task_summary=REPO_SUMMARY)
        self.assertEqual(result.state, "simulation_completed")
        self.assertEqual(len(harness.calls), 3)
        self.assertEqual({call["task_id"] for call in harness.calls}, {REPO_TASK_ID})
        self.assertEqual(self.of("run_started")[0]["task_id"], REPO_TASK_ID)
        roles_seen = set()
        for _, role, messages in self.client.calls:
            roles_seen.add(role)
            text = json.dumps(messages)
            self.assertIn(REPO_SUMMARY, text)
            self.assertNotIn("logs", text)
            if role != "verifier":  # the verifier prompt embeds the fixture candidate source
                self.assertNotIn("solve(lines)", text)
        self.assertEqual(roles_seen, {"planner", "worker_a", "worker_b", "verifier"})

    def test_default_task_id_is_logs(self):
        self.orchestrate()
        self.assertEqual({call["task_id"] for call in self.harness.calls}, {"logs-parse-v1"})
        self.assertEqual(self.of("run_started")[0]["task_id"], "logs-parse-v1")

    def test_task_id_mismatch_is_failed_infrastructure(self):
        harness = FakeHarness(harness_script(), task_id="repo-000000000000")
        result = self.orchestrate(harness=harness, task_id=REPO_TASK_ID, task_summary=REPO_SUMMARY)
        self.assertEqual(result.state, "failed_infrastructure")
        self.assertEqual(self.of("harness_error")[0]["error_type"], "ValueError")
        self.assertEqual(harness.calls, [])

    def test_bad_task_id_is_rejected(self):
        for bad in ("", "x" * 129, None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.orchestrate(task_id=bad)
        with self.assertRaises(ValueError):
            FakeHarness(task_id="")

    def test_parser_error_names_a_generic_solve(self):
        from agent_team.models import MalformedOutput
        from agent_team.roles import parse_candidate
        with self.assertRaisesRegex(MalformedOutput, r"solve\(<one argument>\)"):
            parse_candidate("```python\ndef other(x):\n    return x\n```")
        parse_candidate("```python\ndef solve(data):\n    return data\n```")


class PrivacyTests(OrchestrationCase):
    def test_session_id_and_seed_never_reach_models(self):
        secret = "SECRET-SESSION-xyz"
        result = self.orchestrate(
            session_id=secret, max_rounds=2,
            client_script=script(**{"v-primary": [verdict(SHA_A, approved=False)]}))
        self.assertEqual(result.state, "rounds_exhausted")
        self.assertTrue(self.client.calls)
        for model_id, role, messages in self.client.calls:
            blob = json.dumps(messages).lower()
            self.assertNotIn(secret.lower(), blob)
            self.assertNotIn("seed", blob)
        self.assertEqual(self.harness.calls[0]["session_id"], secret)

    def test_feedback_forwards_only_allowlisted_fields(self):
        harness = FakeHarness(harness_script(**{"worker_b-r1": {"screen": {
            "screen_median_speedup": 2.0,
            "feedback_public": [{"code": "slow", "summary": "public summary",
                                 "evidence_ids": ["ev-1"]}]}}}))
        client_script = script(**{"v-primary": [verdict(SHA_A, approved=False)]})
        self.orchestrate(client_script, harness=harness, max_rounds=2)
        round_two = json.dumps([c for c in self.client.calls if c[1] == "worker_b"][1][2])
        self.assertIn("public summary", round_two)
        self.assertNotIn("request_id", round_two)
        self.assertNotIn("req-", round_two)


class SafetyTests(OrchestrationCase):
    def test_annotation_spoofing_is_rejected(self):
        bad = [{"type": "terminal", "state": "verified_done", "run_id": "spoofed"},
               {"type": "verifier_verdict", "approved": True},
               {"type": "fault_injection", "round": 3},
               {"type": "fault_injection", "rnd": 1}, {"type": "fault_injection", "kind": "x"},
               {"type": "fault_injection", "run_id": "spoofed"},
               {"type": "simulation_label", "utc": "2020-01-01T00:00:00Z"},
               {"type": "fault_injection", "label": ["nested"]},
               {"type": "fault_injection", "label": {"a": 1}},
               {"type": "fault_injection", "label": float("nan")},
               {"type": "fault_injection", 5: "label"},
               {"label": "no type"}, "not a dict"]
        for item in bad:
            with self.subTest(item=item):
                with self.assertRaises(ValueError):
                    self.orchestrate(annotations=[item])
        self.assertFalse((self.dir / "ledger.jsonl").exists())

    def test_allowed_annotations_are_logged_first_and_labeled(self):
        self.orchestrate(annotations=[{"type": "fault_injection", "label": "drill", "count": 2},
                                      {"type": "simulation_label", "label": "synthetic"}])
        self.assertEqual(self.types()[:3], ["run_started", "fault_injection", "simulation_label"])
        self.assertEqual(self.types().count("terminal"), 1)
        self.assertEqual(self.result.events[1]["run_id"], self.result.run_id)

    def test_preflight_runs_before_any_model_call(self):
        seen = []
        self.orchestrate(preflight=lambda: seen.append(list(self.client.calls)))
        self.assertEqual(seen, [[]])
        self.assertEqual(self.of("preflight")[0]["ok"], True)
        self.assertEqual(self.types().index("preflight"), 1)

    def test_preflight_failure_is_terminal_without_model_calls(self):
        def preflight():
            raise RuntimeError("session not prepared at /secret/path")

        result = self.orchestrate(preflight=preflight)
        self.assertEqual(result.state, "failed_infrastructure")
        self.assertEqual(self.of("terminal")[0]["reason"], "preflight failed: RuntimeError")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.harness.calls, [])
        self.assertEqual(self.of("plan"), [])
        self.assertNotIn("/secret/path", (self.dir / "ledger.jsonl").read_text())

    def test_unexpected_exception_is_bounded_and_diagnosed_off_ledger(self):
        class Exploding:
            calls = []

            def complete(self, *args):
                raise RuntimeError("client bug with SECRET-DETAIL")

        result = self.orchestrate(client=Exploding(), allow_unexpected=True)
        self.assertEqual(result.state, "failed_infrastructure")
        self.assertEqual(self.of("terminal")[0]["reason"], "unexpected RuntimeError")
        diagnostics = (self.dir / "diagnostics.log").read_text()
        self.assertIn("Traceback", diagnostics)
        self.assertIn("SECRET-DETAIL", diagnostics)
        ledger = (self.dir / "ledger.jsonl").read_text()
        self.assertNotIn("Traceback", ledger)
        self.assertNotIn("SECRET-DETAIL", ledger)
        verify_ledger(result.ledger_path)

    def test_no_diagnostics_file_on_clean_run(self):
        self.orchestrate()
        self.assertFalse((self.dir / "diagnostics.log").exists())


class BeforeAttemptHookTests(unittest.TestCase):
    ORDER = ModelOrder("primary", "fallback")

    def call(self, fixture, hook, budget=None):
        return call_with_fallback(self.ORDER, fixture, budget or Budget(), role="w",
                                  messages=[{"role": "user", "content": "hi"}],
                                  parse=lambda text: text, before_attempt=hook)

    def test_hook_runs_before_each_attempt_and_stops_calls(self):
        fixture = FixtureModelClient({"primary": [ModelError("x"), ModelError("x")],
                                      "fallback": ["done"]})
        budget, seen = Budget(), []

        def hook():
            seen.append(budget.attempts_used)
            if len(seen) == 2:
                raise TimeoutError("stop")

        with self.assertRaises(TimeoutError) as caught:
            self.call(fixture, hook, budget)
        self.assertEqual(seen, [0, 1])  # ran before each reservation
        self.assertEqual([c[0] for c in fixture.calls], ["primary"])
        self.assertEqual([a["kind"] for a in caught.exception.attempts], ["primary"])
        self.assertEqual(budget.attempts_used, 1)

    def test_hook_optional_and_passing_hook_is_harmless(self):
        fixture = FixtureModelClient({"primary": ["ok"]})
        self.assertEqual(self.call(fixture, None)[0], "ok")
        fixture = FixtureModelClient({"primary": ["ok"]})
        self.assertEqual(self.call(fixture, lambda: None)[0], "ok")


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(list(args))
        return code, stdout.getvalue()

    def test_simulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            code, text = self.run_cli("simulate", "--output", str(out))
            summary = json.loads(text)
            self.assertEqual(code, 0)
            self.assertEqual(summary["state"], "simulation_completed")
            self.assertTrue(summary["ledger_verified"])
            self.assertEqual(summary["last_forwarded_candidate_id"], "worker_a-r1")
            self.assertNotIn("best_candidate_id", summary)
            events = [r["event"] for r in verify_ledger(out / "ledger.jsonl")]
            label = [e for e in events if e["type"] == "simulation_label"]
            self.assertEqual(len(label), 1)
            self.assertIn("never a verified result", label[0]["label"])
            self.assertEqual(events[1]["type"], "simulation_label")
            self.assertEqual(verify_ledger(out / "ledger.jsonl")[-1]["sha256"],
                             summary["ledger_final_sha256"])
            self.assertEqual(self.run_cli("simulate", "--output", str(out))[0], 2)

    def test_simulate_with_injected_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            code, text = self.run_cli("simulate", "--output", str(out), "--inject-failures")
            summary = json.loads(text)
            self.assertEqual((code, summary["state"]), (0, "simulation_completed"))
            self.assertTrue(summary["ledger_verified"])
            events = [r["event"] for r in verify_ledger(out / "ledger.jsonl")]
            self.assertEqual(events[1]["type"], "fault_injection")
            self.assertIn("injected", events[1]["label"])
            self.assertEqual(events[0]["type"], "run_started")

    def test_invalid_deadline_exits_before_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("0", "-5", "1801", "nan", "inf"):
                out = Path(tmp) / f"run{value}"
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code, text = self.run_cli("simulate", "--output", str(out), "--deadline", value)
                self.assertEqual(code, 2, value)
                self.assertEqual(text, "")
                self.assertFalse(out.exists(), value)
                self.assertIn("--deadline", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
            out = Path(tmp) / "max"
            self.assertEqual(self.run_cli("simulate", "--output", str(out), "--deadline", "1800")[0], 0)

    def test_module_entry_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            done = subprocess.run([sys.executable, "-m", "agent_team", "simulate",
                                   "--output", str(out)], cwd=ROOT, capture_output=True,
                                  text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["state"], "simulation_completed")


if __name__ == "__main__":
    unittest.main()
