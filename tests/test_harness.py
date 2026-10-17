from pathlib import Path
import tempfile
import unittest

from optimizer_core.backend import TrialResult
from optimizer_core.canonical import canonical_json, digest, strict_json
from optimizer_core.harness import Harness, policy_findings
from optimizer_core.receipts import audit_receipt
from optimizer_core.tasks import BASELINE_SOURCE, REFERENCE_SOURCE, baseline, reference


class FakeBackend:
    """Produces synthetic data; never execs candidate source or opens a socket."""
    origin = "synthetic"
    environment_sha256 = digest(b"unit-test-runtime")
    def __init__(self, *, wrong=False, cleanup=True):
        self.calls, self.wrong, self.cleanup = [], wrong, cleanup

    def run_trial(self, request):
        self.calls.append(request)
        cases = strict_json(request.input_bytes)["cases"]
        wire = {"results": [{"id": case["id"], "output": (
            {} if self.wrong else reference(case["input"]))} for case in cases]}
        proof = canonical_json({"origin": "synthetic", "trial_id": request.trial_id})
        return TrialResult(
            request.trial_id, canonical_json(wire), 0,
            1.0 if request.candidate_source == BASELINE_SOURCE else 0.1,
            self.environment_sha256, "fake-sandbox", digest(proof), self.cleanup,
            proof_bytes=proof, origin="synthetic")


def request(mode="screen", source=REFERENCE_SOURCE):
    return {"schema_version": 1, "request_id": "unit-req", "session_id": "unit-session",
            "task_id": "logs-parse-v1",
            "candidate": {"candidate_id": "unit-candidate", "source_utf8": source.decode(),
                          "sha256": digest(source)},
            "mode": mode, "remaining_wall_seconds": 900,
            "verifier": {"approved": True, "evidence_ids": ["fixture-verdict"]}}


class HarnessTests(unittest.TestCase):
    def run_harness(self, req, *, wrong=False, cleanup=True):
        backend = FakeBackend(wrong=wrong, cleanup=cleanup)
        harness = Harness(backend, output=self.directory.name, seed="unit-seed",
                          workload_lines=20, hidden_cases=3)
        return harness.evaluate_candidate(req), backend

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_screen_never_accepts_even_with_fast_results(self):
        result, backend = self.run_harness(request())
        self.assertEqual(result["status"], "screened")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["screen_median_speedup"], 10)
        self.assertEqual(len(backend.calls), 11)  # one correctness batch + five pairs

    def test_full_synthetic_receipt_is_auditable_but_never_accepted(self):
        result, backend = self.run_harness(request("acceptance"))
        self.assertEqual(result["status"], "simulated_acceptance")
        self.assertFalse(result["accepted"])
        self.assertEqual(len(backend.calls), 71)
        path = Path(self.directory.name) / result["receipt_id"] / "receipt.json"
        self.assertFalse(audit_receipt(
            path, expected_sha256=result["receipt_sha256"])["accepted"])

    def test_wrong_output_is_rejected_without_leaking_private_values(self):
        result, _ = self.run_harness(request(), wrong=True)
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["correctness_passed"])
        self.assertEqual(result["feedback_public"][0]["code"], "output_or_schema_mismatch")
        self.assertNotIn("latency", str(result["feedback_public"]))

    def test_cleanup_failure_is_infrastructure_error(self):
        result, _ = self.run_harness(request(), cleanup=False)
        self.assertEqual(result["status"], "infrastructure_error")
        self.assertFalse(result["accepted"])

    def test_incomplete_verifier_blocks_before_execution(self):
        req = request("acceptance")
        req["verifier"] = {"approved": True, "evidence_ids": []}
        result, backend = self.run_harness(req)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(backend.calls, [])

    def test_source_hash_mismatch_rejected(self):
        req = request()
        req["candidate"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.run_harness(req)

    def test_clock_patch_rejected_before_any_execution(self):
        req = request(source=REFERENCE_SOURCE + b"\nimport time as t\nt.perf_counter=lambda:0\n")
        result, backend = self.run_harness(req)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(backend.calls, [])

    def test_short_budget_stops_without_execution(self):
        req = request()
        req["remaining_wall_seconds"] = 1
        result, backend = self.run_harness(req)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(backend.calls, [])

    def test_baseline_and_oracle_match_edges(self):
        cases = [[], [""], ["INFO user=a latency=00003"] * 3,
                 ["ERROR user=x latency=9", "INFO user=x latency=1",
                  "INFO user=é latency=1", "INFO user=a latency=1\n"]]
        for lines in cases:
            self.assertEqual(baseline(lines), reference(lines))

    def test_honest_sleep_does_not_trigger_clock_patch(self):
        self.assertEqual(policy_findings(
            REFERENCE_SOURCE + b"\nimport time\ntime.sleep(0.001)\n"), [])
