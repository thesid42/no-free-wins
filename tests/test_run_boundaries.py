"""Regression checks for real-run deadlines and potentially billable failed calls."""
import json
from pathlib import Path
import tempfile
import unittest

from agent_team.budget import Budget, BudgetExhausted, estimate_prompt_tokens
from agent_team.calls import ModelOrder, call_with_fallback
from agent_team.crusoe import CrusoeClient, prices_for
from agent_team.models import FixtureModelClient, ModelError, ModelReply
from agent_team.orchestrator import Orchestrator
from agent_team.fake_harness import FakeHarness
from test_orchestration import FakeClock, ORDERS, harness_script, measured_acceptance, script


class RunBoundaryTests(unittest.TestCase):
    def test_late_measured_acceptance_cannot_finish_verified(self):
        clock = FakeClock()
        fake = FakeHarness(harness_script())
        def evaluate(request):
            if request["mode"] == "acceptance":
                clock.now = 1801
                return measured_acceptance(request)
            return fake.evaluate_candidate(request)
        with tempfile.TemporaryDirectory() as directory:
            result = Orchestrator(
                evaluate=evaluate, client=FixtureModelClient(script()), budget=Budget(),
                ledger_path=Path(directory) / "ledger.jsonl", session_id="boundary-test",
                orders=ORDERS, deadline_seconds=1800, clock=clock).run()
        self.assertEqual(result.state, "failed_deadline")
        self.assertTrue(any(e["type"] == "harness_returned_late" for e in result.events))
        self.assertFalse(any(e["type"] == "acceptance_result" for e in result.events))

    def test_late_verifier_cannot_submit_acceptance(self):
        clock = FakeClock()
        inner = FixtureModelClient(script())
        fake = FakeHarness(harness_script())
        class Client:
            def complete(self, model, role, messages, max_tokens):
                if role == "verifier":
                    clock.now = 1801
                return inner.complete(model, role, messages, max_tokens)
        with tempfile.TemporaryDirectory() as directory:
            result = Orchestrator(
                evaluate=fake.evaluate_candidate, client=Client(), budget=Budget(),
                ledger_path=Path(directory) / "ledger.jsonl", session_id="boundary-test",
                orders=ORDERS, deadline_seconds=1800, clock=clock).run()
        self.assertEqual(result.state, "failed_deadline")
        self.assertEqual([r["mode"] for r in fake.calls], ["screen", "screen"])


class UsageAndTransportTests(unittest.TestCase):
    primary = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
    fallback = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    messages = [{"role": "user", "content": "x"}]

    def test_empty_reply_usage_is_charged_and_stops_retry(self):
        requests = []
        def opener(*args):
            requests.append(args)
            return 200, json.dumps({"choices": [{"message": {"content": ""},
                                                "finish_reason": "length"}],
                                    "usage": {"total_tokens": 200}}).encode()
        client = CrusoeClient(api_key="unit-placeholder", opener=opener)
        budget = Budget(prices=prices_for([self.primary, self.fallback]), max_cost_usd=0.0007)
        with self.assertRaises(BudgetExhausted):
            call_with_fallback(ModelOrder(self.primary, self.fallback), client, budget,
                               role="worker", messages=self.messages, max_tokens=256,
                               parse=lambda text: text)
        self.assertEqual(len(requests), 1)
        self.assertEqual(budget.tokens_used, 200)
        self.assertAlmostEqual(budget.cost_used, 0.00048)
        self.assertEqual(budget.snapshot()["open_reservations"], 0)

    def test_unknown_failed_usage_retains_reservation(self):
        client = FixtureModelClient({"p": [ModelError("timeout"), "unused"]})
        budget = Budget(max_tokens=300)
        with self.assertRaises(BudgetExhausted):
            call_with_fallback(ModelOrder("p", "f"), client, budget, role="worker",
                               messages=self.messages, max_tokens=256, parse=lambda text: text)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(budget.tokens_used, estimate_prompt_tokens(self.messages) + 256)
        self.assertEqual(budget.snapshot()["open_reservations"], 0)

    def test_reported_overrun_stops_before_parsing_result(self):
        client = FixtureModelClient({"p": [ModelReply("verdict", 1001)]})
        parsed = []
        budget = Budget(max_tokens=1000)
        with self.assertRaises(BudgetExhausted):
            call_with_fallback(ModelOrder("p", "f"), client, budget, role="verifier",
                               messages=self.messages, max_tokens=256,
                               parse=lambda text: parsed.append(text))
        self.assertEqual(parsed, [])
        self.assertTrue(budget.snapshot()["overrun"])

    def test_remaining_deadline_reaches_real_transport(self):
        timeouts = []
        def opener(method, url, headers, body, timeout):
            timeouts.append(timeout)
            return 200, json.dumps({"choices": [{"message": {"content": "ok"}}],
                                    "usage": {"total_tokens": 2}}).encode()
        client = CrusoeClient(api_key="unit-placeholder", timeout=120, opener=opener)
        value, _ = call_with_fallback(
            ModelOrder("p", "f"), client, Budget(), role="worker", messages=self.messages,
            max_tokens=256, parse=lambda text: text, before_attempt=lambda: 7)
        self.assertEqual(value, "ok")
        self.assertEqual(timeouts, [7])

    def test_reported_cost_overrun_stops_before_parsing(self):
        client = FixtureModelClient({"p": [ModelReply("verdict", 1000)]})
        budget = Budget(prices={"p": 0.01}, max_cost_usd=0.004)
        parsed = []
        with self.assertRaises(BudgetExhausted):
            call_with_fallback(ModelOrder("p", "f"), client, budget, role="verifier",
                               messages=self.messages, max_tokens=256,
                               parse=lambda text: parsed.append(text))
        self.assertEqual(parsed, [])
        self.assertEqual(budget.tokens_used, 1000)
        self.assertTrue(budget.snapshot()["overrun"])
