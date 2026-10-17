import tempfile
import unittest
from pathlib import Path

from agent_team.budget import Budget
from agent_team.calls import ModelOrder, call_with_fallback
from agent_team.fake_harness import FakeHarness
from agent_team.faults import (INJECTED_PREFIX, FaultInjectingClient, drill_annotation,
                               drill_plan)
from agent_team.models import FixtureModelClient, ModelReply
from agent_team.orchestrator import Orchestrator, _check_annotation
from optimizer_core.ledger import verify_ledger
from tests.test_orchestration import FakeClock, ORDERS, fence, harness_script, script
from agent_team.__main__ import SOURCE_A

MESSAGES = [{"role": "user", "content": "hello"}]


def run_order(client, budget=None, **kwargs):
    budget = budget or Budget()
    parsed, attempts = call_with_fallback(ModelOrder(primary="p", fallback="f"), client, budget,
                                          role="worker_a", messages=MESSAGES, parse=lambda t: t,
                                          **kwargs)
    return parsed, attempts, budget


class TimedInner:
    def __init__(self):
        self.timeouts = []

    def complete(self, model_id, role, messages, max_tokens):
        return ModelReply("plain", 1)

    def complete_with_timeout(self, model_id, role, messages, max_tokens, timeout_seconds):
        self.timeouts.append(timeout_seconds)
        return ModelReply("timed", 1)

    def list_models(self):
        return ["m"]


class FallbackRecoveryTests(unittest.TestCase):
    def test_fallback_recovers_after_two_injected_failures(self):
        inner = FixtureModelClient({"p": ["never"], "f": ["recovered"]})
        client = FaultInjectingClient(inner, faults={"p": 2})
        parsed, attempts, budget = run_order(client)
        self.assertEqual(parsed, "recovered")
        self.assertEqual([(a["kind"], a["outcome"]) for a in attempts],
                         [("primary", "model_error"), ("retry", "model_error"),
                          ("fallback", "ok")])
        self.assertTrue(all(a["detail"].startswith("injected fault") for a in attempts[:2]))
        self.assertEqual([a["tokens"] for a in attempts[:2]], [0, 0])
        self.assertTrue(all(a["usage_known"] for a in attempts[:2]))
        self.assertEqual([c[0] for c in inner.calls], ["f"])
        self.assertEqual(budget.tokens_used, attempts[2]["tokens"])
        self.assertEqual(inner.script["p"], ["never"])

    def test_injected_error_is_labeled_with_no_usage(self):
        client = FaultInjectingClient(FixtureModelClient({}), faults={"p": 1})
        with self.assertRaises(Exception) as ctx:
            client.complete("p", "worker_a", MESSAGES, 10)
        self.assertTrue(str(ctx.exception).startswith(INJECTED_PREFIX))
        self.assertIn("no request sent", str(ctx.exception))
        self.assertEqual(ctx.exception.tokens, 0)

    def test_log_and_injected_total(self):
        inner = FixtureModelClient({"p": ["late"], "f": ["ok-f"]})
        client = FaultInjectingClient(inner, faults={"p": 1})
        self.assertEqual(client.injected_total, 0)
        with self.assertRaises(Exception):
            client.complete("p", "r", MESSAGES, 10)
        client.complete("p", "r", MESSAGES, 10)
        client.complete("f", "r", MESSAGES, 10)
        self.assertEqual(client.log, [
            {"model_id": "p", "call_index": 1, "injected": True},
            {"model_id": "p", "call_index": 2, "injected": False},
            {"model_id": "f", "call_index": 1, "injected": False}])
        self.assertEqual(client.injected_total, 1)
        self.assertNotIn("hello", repr(client.log))


class PassthroughTests(unittest.TestCase):
    def test_complete_with_timeout_reaches_inner_with_timeout(self):
        inner = TimedInner()
        client = FaultInjectingClient(inner, faults={"p": 1})
        self.assertTrue(hasattr(client, "complete_with_timeout"))
        with self.assertRaises(Exception):
            client.complete_with_timeout("p", "r", MESSAGES, 10, 7.5)
        self.assertEqual(inner.timeouts, [])
        self.assertEqual(client.complete_with_timeout("p", "r", MESSAGES, 10, 7.5).text, "timed")
        self.assertEqual(inner.timeouts, [7.5])
        self.assertEqual(client.list_models(), ["m"])

    def test_timeout_reaches_inner_through_call_with_fallback(self):
        inner = TimedInner()
        client = FaultInjectingClient(inner, faults={"p": 2})
        parsed, _, _ = run_order(client, before_attempt=lambda: 12.0)
        self.assertEqual((parsed, inner.timeouts), ("timed", [12.0]))

    def test_missing_optional_methods_are_not_exposed(self):
        client = FaultInjectingClient(FixtureModelClient({}), faults={"p": 1})
        self.assertFalse(hasattr(client, "complete_with_timeout"))
        self.assertFalse(hasattr(client, "list_models"))
        with self.assertRaises(AttributeError):
            client.nonexistent


class ValidationTests(unittest.TestCase):
    def test_bad_faults_are_rejected(self):
        inner = FixtureModelClient({})
        for faults in ({}, None, [("p", 1)], {"p": 0}, {"p": 4}, {"p": True}, {"p": 1.0},
                       {"p": "1"}, {"": 1}, {5: 1}):
            with self.subTest(faults=faults):
                with self.assertRaises(ValueError):
                    FaultInjectingClient(inner, faults=faults)

    def test_drill_plan(self):
        self.assertEqual(drill_plan(ORDERS), {"a-primary": 2})
        self.assertEqual(drill_plan(ORDERS, role="verifier", failures=1), {"v-primary": 1})

    def test_drill_plan_rejects_bad_input(self):
        for kwargs in ({"role": "nobody"}, {"failures": 3}, {"failures": 0},
                       {"failures": True}, {"failures": "2"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    drill_plan(ORDERS, **kwargs)
        with self.assertRaises(ValueError):
            drill_plan(None)

    def test_drill_annotation_passes_orchestrator_check(self):
        item = drill_annotation("a-primary", 2)
        _check_annotation(item)
        self.assertEqual(item["type"], "fault_injection")
        self.assertEqual(item["model_id"], "a-primary")
        self.assertEqual(item["failures"], 2)
        self.assertIn("labeled drill", item["label"])
        self.assertTrue(all(type(v) in (str, int) for v in item.values()))
        for args in (("", 2), (5, 2), ("m", 0), ("m", 4)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    drill_annotation(*args)


class EndToEndTests(unittest.TestCase):
    def test_orchestrated_run_recovers_through_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            inner = FixtureModelClient(script(**{"a-primary": [],
                                                 "a-fallback": [fence(SOURCE_A)]}))
            faults = drill_plan(ORDERS)
            client = FaultInjectingClient(inner, faults=faults)
            harness = FakeHarness(harness_script())
            ids = iter(range(1, 10_000))
            orch = Orchestrator(
                evaluate=harness.evaluate_candidate, client=client, budget=Budget(),
                ledger_path=Path(tmp) / "ledger.jsonl", session_id="session-1", orders=ORDERS,
                deadline_seconds=900, clock=FakeClock(), utcnow=lambda: "2026-10-17T00:00:00Z",
                new_id=lambda: f"{next(ids):04d}",
                annotations=[drill_annotation("a-primary", 2)])
            result = orch.run()
            self.assertEqual(result.state, "simulation_completed")
            types = [e["type"] for e in result.events]
            self.assertEqual(types[:2], ["run_started", "fault_injection"])
            self.assertEqual(result.events[1]["model_id"], "a-primary")
            created = [e for e in result.events
                       if e["type"] == "candidate_created" and e["worker"] == "worker_a"][0]
            self.assertEqual([(a["kind"], a["model_id"], a["outcome"])
                              for a in created["attempts"]],
                             [("primary", "a-primary", "model_error"),
                              ("retry", "a-primary", "model_error"),
                              ("fallback", "a-fallback", "ok")])
            self.assertTrue(all(a["detail"].startswith("injected fault")
                                for a in created["attempts"][:2]))
            self.assertEqual(client.injected_total, 2)
            self.assertNotIn("a-primary", [c[0] for c in inner.calls])
            verify_ledger(result.ledger_path)


if __name__ == "__main__":
    unittest.main()
