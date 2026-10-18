import hashlib
import json
import tempfile
import unittest

from agent_team.budget import Budget, BudgetExhausted, estimate_prompt_tokens
from agent_team.calls import ModelOrder, OrderFailed, call_with_fallback
from agent_team.contract import (build_request, is_verified_done, make_candidate,
                                 validate_response)
from agent_team.fake_harness import FakeHarness
from agent_team.models import FixtureModelClient, MalformedOutput, ModelError, ModelReply
from agent_team.roles import (distinct_strategies, parse_candidate, parse_plan, parse_verdict,
                              public_feedback, verifier_messages, worker_messages, TASK_SUMMARY)
from optimizer_core.backend import TrialResult
from optimizer_core.canonical import canonical_json, digest, strict_json
from optimizer_core.harness import Harness
from optimizer_core.tasks import REFERENCE_SOURCE, reference

GOOD = "def solve(lines):\n    return {}\n"


class FakeBackend:
    """Produces synthetic data; never execs candidate source or opens a socket."""
    origin = "synthetic"
    environment_sha256 = digest(b"unit-test-runtime")

    def run_trial(self, request):
        cases = strict_json(request.input_bytes)["cases"]
        wire = {"results": [{"id": case["id"], "output": reference(case["input"])}
                            for case in cases]}
        proof = canonical_json({"origin": "synthetic", "trial_id": request.trial_id})
        return TrialResult(
            request.trial_id, canonical_json(wire), 0, 0.1, self.environment_sha256,
            "fake-sandbox", digest(proof), True, proof_bytes=proof, origin="synthetic")


def req(mode="screen", source=GOOD, candidate_id="worker-a-r1", verifier=None):
    if mode == "acceptance" and verifier is None:
        verifier = {"approved": True, "evidence_ids": ["verdict-1"]}
    return build_request(request_id="req-1", session_id="sess-1",
                         candidate=make_candidate(candidate_id, source), mode=mode,
                         remaining_wall_seconds=900, verifier=verifier)


def measured(**changes):
    resp = {"schema_version": 1, "request_id": "req-1", "candidate_id": "c", "status": "accepted",
            "origin": "measured", "accepted": True, "correctness_passed": True,
            "screen_median_speedup": None,
            "acceptance": {"accepted": True, "origin": "measured", "pairs": 30,
                           "median_speedup": 3.4, "ci_lower": 3.1, "ci_upper": 3.7,
                           "reasons": []},
            "receipt_id": "r1", "receipt_sha256": "a" * 64, "evidence_ids": ["r1"],
            "feedback_public": []}
    resp.update(changes)
    return resp


class ContractTests(unittest.TestCase):
    def test_candidate_hash_uses_exact_bytes(self):
        crlf, lf = "def solve(lines):\r\n    return {}\r\n", GOOD
        self.assertEqual(make_candidate("c", crlf)["sha256"],
                         hashlib.sha256(crlf.encode("utf-8")).hexdigest())
        self.assertNotEqual(make_candidate("c", crlf)["sha256"], make_candidate("c", lf)["sha256"])
        self.assertEqual(make_candidate("c", crlf)["source_utf8"], crlf)

    def test_request_accepted_by_real_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(FakeBackend(), output=directory, seed="unit-seed",
                              workload_lines=20, hidden_cases=3)
            request = req(source=REFERENCE_SOURCE.decode())
            response = harness.evaluate_candidate(request)
        self.assertEqual((response["status"], response["origin"]), ("screened", "synthetic"))
        self.assertIs(validate_response(response, request), response)

    def test_real_harness_acceptance_mode_is_synthetic(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(FakeBackend(), output=directory, seed="unit-seed",
                              workload_lines=20, hidden_cases=3)
            request = req("acceptance", source=REFERENCE_SOURCE.decode())
            response = harness.evaluate_candidate(request)
        self.assertEqual(response["status"], "simulated_acceptance")
        self.assertIs(validate_response(response, request), response)
        self.assertFalse(is_verified_done(response))

    def test_build_request_rejects_bad_identifiers_and_envelopes(self):
        good = make_candidate("c", GOOD)
        base = {"request_id": "r", "session_id": "s", "candidate": good,
                "mode": "screen", "remaining_wall_seconds": 10}
        for change in ({"request_id": ""}, {"session_id": "x" * 129}, {"request_id": 1},
                       {"candidate": {**good, "extra": 1}}, {"candidate": "c"},
                       {"candidate": {**good, "candidate_id": ""}},
                       {"candidate": {**good, "source_utf8": b"x"}},
                       {"candidate": {**good, "sha256": "Z" * 64}},
                       {"candidate": {**good, "sha256": "ab"}}):
            with self.assertRaises(ValueError, msg=change):
                build_request(**{**base, **change})

    def test_build_request_validation(self):
        candidate = make_candidate("c", GOOD)
        for kwargs in ({"mode": "acceptance"}, {"mode": "other"},
                       {"mode": "screen", "remaining_wall_seconds": 0},
                       {"mode": "screen", "remaining_wall_seconds": float("inf")},
                       {"mode": "acceptance", "verifier": {"approved": True, "evidence_ids": []}},
                       {"mode": "acceptance", "verifier": {"approved": False, "evidence_ids": ["x"]}}):
            args = {"request_id": "r", "session_id": "s", "candidate": candidate,
                    "remaining_wall_seconds": 10, **kwargs}
            with self.assertRaises(ValueError, msg=kwargs):
                build_request(**args)
        self.assertEqual(len(req()), 8)

    def test_validate_response_rejects_violations(self):
        request, good = req(), FakeHarness().evaluate_candidate(req())
        for change in ({"status": "bogus"}, {"origin": "real"}, {"accepted": 0},
                       {"request_id": "other"}, {"candidate_id": "other"},
                       {"extra": 1}, {"feedback_public": [{"code": "x"}]}):
            with self.assertRaises(ValueError, msg=change):
                validate_response({**good, **change}, request)
        with self.assertRaises(ValueError):
            validate_response({**good, "schema_version": True}, request)
        for change in ({"status": ["screened"]}, {"origin": ["synthetic"]}, {"status": {}}):
            with self.assertRaises(ValueError, msg=change):
                validate_response({**good, **change}, request)

    def test_is_verified_done(self):
        self.assertTrue(is_verified_done(measured()))
        self.assertFalse(is_verified_done(measured(origin="synthetic")))
        self.assertFalse(is_verified_done(measured(status="simulated_acceptance", origin="synthetic")))
        self.assertFalse(is_verified_done(measured(receipt_sha256=None)))
        self.assertFalse(is_verified_done(measured(receipt_sha256="A" * 64)))
        for field, value in (("pairs", 29), ("ci_lower", 1.0), ("median_speedup", 1.0),
                             ("ci_lower", 0.98), ("median_speedup", 0.9)):
            resp = measured()
            resp["acceptance"][field] = value
            self.assertFalse(is_verified_done(resp))
        for median_speedup, ci_lower in ((1.2, 1.1), (3.4, 1.01)):
            resp = measured()
            resp["acceptance"].update(median_speedup=median_speedup, ci_lower=ci_lower)
            self.assertTrue(is_verified_done(resp))
        # a recorded legacy policy keeps its own 3.0 bar; unknown or absent policies mean 1.0
        for policy_min, expected in ((3.0, False), (1.0, True), (2.0, True), (None, True)):
            resp = measured()
            resp["acceptance"].update(median_speedup=2.5, ci_lower=2.4,
                                      policy={"minimum_speedup": policy_min})
            self.assertIs(is_verified_done(resp), expected)
        for field, value in (("origin", "synthetic"), ("reasons", ["synthetic_evidence"])):
            resp = measured()
            resp["acceptance"][field] = value
            self.assertFalse(is_verified_done(resp))
        for junk in (None, [], {}, {"status": "accepted"}, measured(acceptance=[])):
            self.assertFalse(is_verified_done(junk))


class FakeHarnessTests(unittest.TestCase):
    def test_screen_default(self):
        fake = FakeHarness()
        resp = fake.evaluate_candidate(req())
        self.assertEqual((resp["status"], resp["origin"], resp["accepted"]),
                         ("screened", "synthetic", False))
        self.assertEqual((resp["correctness_passed"], resp["screen_median_speedup"]), (True, 1.0))
        self.assertEqual(resp["evidence_ids"], ["fixture-screen-001"])
        self.assertEqual(len(fake.calls), 1)

    def test_acceptance_without_verifier_is_rejected(self):
        request = req("acceptance")
        request["verifier"] = None
        resp = FakeHarness().evaluate_candidate(request)
        self.assertEqual(resp["status"], "rejected")
        self.assertEqual(resp["feedback_public"][0]["code"], "verifier_evidence_required")

    def test_acceptance_with_verifier_is_simulated(self):
        resp = FakeHarness().evaluate_candidate(req("acceptance"))
        self.assertEqual(resp["status"], "simulated_acceptance")
        self.assertFalse(resp["accepted"])
        self.assertEqual((resp["acceptance"]["accepted"], resp["acceptance"]["pairs"]), (False, 30))
        self.assertEqual(resp["acceptance"]["reasons"], ["synthetic_evidence"])
        self.assertFalse(is_verified_done(resp))
        self.assertEqual(resp["receipt_id"], "fixture-receipt-001")
        self.assertEqual(resp["receipt_sha256"], digest(canonical_json(resp["acceptance"])))
        self.assertEqual(resp["acceptance"]["policy"]["bootstrap_seed"], 42)

    def test_script_cannot_claim_real_acceptance(self):
        fake = FakeHarness({"worker-a-r1": {"acceptance": {
            "origin": "measured", "accepted": True, "status": "accepted",
            "acceptance": {"accepted": True, "origin": "measured", "median_speedup": 9.0}}}})
        resp = fake.evaluate_candidate(req("acceptance"))
        self.assertEqual((resp["origin"], resp["accepted"]), ("synthetic", False))
        self.assertEqual(resp["status"], "simulated_acceptance")
        self.assertEqual((resp["acceptance"]["origin"], resp["acceptance"]["accepted"]),
                         ("synthetic", False))
        self.assertEqual(resp["acceptance"]["median_speedup"], 9.0)
        self.assertFalse(is_verified_done(resp))

    def test_scripted_screen_and_exception(self):
        fake = FakeHarness({"a": {"screen": {"screen_median_speedup": 4.0}},
                            "b": RuntimeError("boom")})
        self.assertEqual(fake.evaluate_candidate(req(candidate_id="a"))["screen_median_speedup"], 4.0)
        with self.assertRaises(RuntimeError):
            fake.evaluate_candidate(req(candidate_id="b"))

    def test_malformed_requests_raise(self):
        fake = FakeHarness()
        bad_hash = req()
        bad_hash["candidate"]["sha256"] = "0" * 64
        extra = req()
        extra["extra"] = 1
        wrong_task = req()
        wrong_task["task_id"] = "other"
        for bad in (bad_hash, extra, wrong_task, "not a dict"):
            with self.assertRaises(ValueError):
                fake.evaluate_candidate(bad)
        self.assertEqual(fake.calls, [])


def client(**queues):
    return FixtureModelClient(queues)


class CallTests(unittest.TestCase):
    order = ModelOrder("p", "f")
    messages = [{"role": "user", "content": "secret prompt"}]

    def run_call(self, fixture, budget=None, parse=lambda text: text):
        return call_with_fallback(self.order, fixture, budget or Budget(), role="worker",
                                  messages=self.messages, parse=parse)

    def test_fallback_after_two_failures(self):
        fixture = client(p=[ModelError("down"), ModelError("down")], f=["hello"])
        result, attempts = self.run_call(fixture)
        self.assertEqual(result, "hello")
        self.assertEqual([a["kind"] for a in attempts], ["primary", "retry", "fallback"])
        self.assertEqual([a["outcome"] for a in attempts], ["model_error", "model_error", "ok"])
        self.assertNotIn("secret prompt", json.dumps(attempts))

    def test_all_attempts_fail(self):
        fixture = client(p=[ModelError("a"), ModelError("b")], f=[ModelError("c")])
        with self.assertRaises(OrderFailed) as caught:
            self.run_call(fixture)
        self.assertEqual(len(caught.exception.attempts), 3)

    def test_malformed_consumes_attempt_then_retry_succeeds(self):
        def parse(text):
            if text != "good":
                raise MalformedOutput("bad shape")
            return text
        fixture = client(p=["junk", "good"])
        result, attempts = self.run_call(fixture, parse=parse)
        self.assertEqual((result, len(attempts)), ("good", 2))
        self.assertEqual(attempts[0]["outcome"], "malformed")
        self.assertEqual(attempts[0]["detail"], "bad shape")
        self.assertEqual(attempts[1]["kind"], "retry")

    def test_budget_exhaustion_stops_before_second_call(self):
        fixture = client(p=[ModelError("down"), "x"], f=["y"])
        with self.assertRaises(BudgetExhausted) as caught:
            self.run_call(fixture, budget=Budget(max_attempts=1))
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(caught.exception.attempts[-1]["outcome"], "budget_exhausted")

    def test_prompt_estimate_counts_against_token_cap(self):
        heavy = [{"role": "user", "content": "x" * 3000}]
        self.assertEqual(estimate_prompt_tokens(heavy), 1001)
        fixture = client(p=["ok"], f=["ok"])
        budget = Budget(max_tokens=1500)
        with self.assertRaises(BudgetExhausted):
            call_with_fallback(self.order, fixture, budget, role="w", messages=heavy,
                               parse=lambda t: t, max_tokens=600)
        self.assertEqual(fixture.calls, [])
        result, _ = call_with_fallback(self.order, fixture, Budget(max_tokens=2500), role="w",
                                       messages=heavy, parse=lambda t: t, max_tokens=600)
        self.assertEqual(result, "ok")

    def test_overrun_blocks_further_reservations(self):
        budget = Budget(max_tokens=100)
        budget.settle(budget.reserve(50), 120)
        self.assertTrue(budget.snapshot()["overrun"])
        with self.assertRaises(BudgetExhausted):
            budget.reserve(1)

    def test_per_model_price_cap_blocks_fallback(self):
        budget = Budget(max_cost_usd=0.5, prices={"p": 0.01, "f": 1.0})
        fixture = client(p=[ModelError("a"), ModelError("b")], f=["never"])
        with self.assertRaises(BudgetExhausted) as caught:
            call_with_fallback(self.order, fixture, budget, role="w", messages=self.messages,
                               parse=lambda t: t, max_tokens=1000)
        self.assertEqual([c[0] for c in fixture.calls], ["p", "p"])
        self.assertEqual(caught.exception.attempts[-1]["kind"], "fallback")
        self.assertTrue(budget.snapshot()["priced"])
        self.assertFalse(Budget().snapshot()["priced"])

    def test_order_requires_distinct_models(self):
        with self.assertRaises(ValueError):
            ModelOrder("same", "same")

    def test_fixture_client_exhaustion_and_replies(self):
        fixture = client(m=["abcdefgh", ModelReply("x", 7)])
        self.assertEqual(fixture.complete("m", "r", [], 10), ModelReply("abcdefgh", 3))
        self.assertEqual(fixture.complete("m", "r", [], 10).tokens, 7)
        with self.assertRaises(ModelError):
            fixture.complete("m", "r", [], 10)
        with self.assertRaises(ModelError):
            fixture.complete("missing", "r", [], 10)


class BudgetTests(unittest.TestCase):
    def test_reserve_and_settle_accounting(self):
        budget = Budget(max_tokens=1000, usd_per_1k_tokens=0.5)
        reservation = budget.reserve(400)
        self.assertEqual((budget.attempts_used, budget.tokens_used), (1, 400))
        budget.settle(reservation, 150)
        snap = budget.snapshot()
        self.assertEqual((snap["attempts_used"], snap["tokens_used"]), (1, 150))
        self.assertAlmostEqual(snap["cost_usd"], 0.075)
        self.assertFalse(snap["overrun"])
        with self.assertRaises(ValueError):
            budget.settle(reservation, 150)
        over = budget.reserve(100)
        budget.settle(over, 300)
        self.assertEqual(budget.tokens_used, 450)

    def test_caps(self):
        budget = Budget(max_tokens=500)
        budget.reserve(300)
        with self.assertRaises(BudgetExhausted):
            budget.reserve(300)
        self.assertEqual(budget.attempts_used, 1)
        with self.assertRaises(BudgetExhausted):
            Budget(task_attempts=0).reserve(1)
        costly = Budget(max_cost_usd=1.0, usd_per_1k_tokens=1.0)
        with self.assertRaises(BudgetExhausted):
            costly.reserve(1001)
        failed = Budget(max_attempts=2)
        failed.reserve(1)
        failed.reserve(1)
        with self.assertRaises(BudgetExhausted):
            failed.reserve(1)


class RoleTests(unittest.TestCase):
    def test_parse_plan(self):
        text = json.dumps({"strategies": [{"strategy_id": "a", "description": "one pass"},
                                          {"strategy_id": "b", "description": "dict"}]})
        plan = parse_plan(text)
        self.assertTrue(distinct_strategies(plan))
        self.assertFalse(distinct_strategies(plan[:1]))
        self.assertFalse(distinct_strategies([plan[0], plan[0]]))
        for bad in ("not json", "[]", '{"strategies": "x"}', '{"strategies": [{"strategy_id": 1}]}'):
            with self.assertRaises(MalformedOutput):
                parse_plan(bad)

    def test_parse_candidate(self):
        source = "def solve(lines):\r\n    return {}\r\n"
        self.assertEqual(parse_candidate(f"Here:\n```python\n{source}```\nDone"), source)
        for bad in ("no fence", f"```python\n{GOOD}```\n```python\n{GOOD}```",
                    "```python\ndef other(lines):\n    pass\n```",
                    "```python\ndef solve(a, b):\n    pass\n```",
                    "```python\ndef solve(lines:\n```"):
            with self.assertRaises(MalformedOutput, msg=bad):
                parse_candidate(bad)
        # Non-python fences elsewhere are tolerated; only python blocks are counted.
        self.assertEqual(parse_candidate(f"```python\n{GOOD}```\n```\nextra\n```"), GOOD)
        self.assertEqual(parse_candidate(f"```text\nnote\n```\n```python\n{GOOD}```"), GOOD)

    def test_parse_verdict(self):
        sha = "b" * 64
        good = json.dumps({"approved": True, "candidate_sha256": sha, "rationale": "ok"})
        self.assertTrue(parse_verdict(good, sha)["approved"])
        mismatch = parse_verdict(good, "c" * 64)
        self.assertEqual((mismatch["approved"], mismatch["mismatch"]), (False, True))
        for bad in ("nope", '{"approved": "yes", "candidate_sha256": "x", "rationale": ""}',
                    '{"approved": true}'):
            with self.assertRaises(MalformedOutput):
                parse_verdict(bad, sha)

    def test_parse_candidate_size_and_encoding(self):
        with self.assertRaises(MalformedOutput):
            parse_candidate(f"```python\n{GOOD}# {'x' * 262_144}\n```")
        with self.assertRaises(MalformedOutput):
            parse_candidate(f"```python\n{GOOD}# \ud800\n```")

    def test_public_feedback_drops_non_str_values(self):
        resp = {"status": ["x"], "screen_median_speedup": float("nan"),
                "feedback_public": [{"code": "ok", "summary": 5, "evidence_ids": ["e", 1, None]},
                                    {"code": 7, "summary": "s", "evidence_ids": []}, "junk"]}
        self.assertEqual(public_feedback([resp, "junk"]), [{
            "status": None, "screen_median_speedup": None,
            "feedback": [{"code": "ok", "summary": "", "evidence_ids": ["e"]}]}])
        self.assertEqual(public_feedback([{"status": "rejected", "screen_median_speedup": 2}])[0]
                         ["screen_median_speedup"], 2)

    def test_worker_messages_sanitize_feedback_themselves(self):
        dirty = [{"code": "c", "summary": "s", "private_seed": "SECRET", "counterexample": "SECRET"},
                 {"status": "rejected", "secret": "SECRET", "feedback": [
                     {"code": "d", "summary": "t", "evidence_ids": ["e"], "hidden": "SECRET"}]},
                 "SECRET"]
        text = worker_messages(TASK_SUMMARY, {"strategy_id": "a", "description": "d"}, dirty)[1]["content"]
        self.assertNotIn("SECRET", text)
        self.assertIn('"code": "c"', text)
        self.assertIn('"code": "d"', text)

    def test_messages_and_public_feedback_allowlist(self):
        self.assertNotIn("seed", TASK_SUMMARY.lower())
        for messages in (worker_messages(TASK_SUMMARY, {"strategy_id": "a", "description": "d"}, []),
                         verifier_messages(TASK_SUMMARY, GOOD, "d" * 64)):
            self.assertEqual([m["role"] for m in messages], ["system", "user"])
        resp = measured(status="rejected", screen_median_speedup=2.0,
                        feedback_public=[{"code": "c", "summary": "s", "evidence_ids": ["e"],
                                          "counterexample": "SECRET"}])
        resp["private_seed"] = "SECRET"
        result = public_feedback([resp])
        self.assertEqual(set(result[0]), {"status", "screen_median_speedup", "feedback"})
        self.assertEqual(set(result[0]["feedback"][0]), {"code", "summary", "evidence_ids"})
        self.assertNotIn("SECRET", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
