from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from optimizer_core.acceptance import AcceptancePolicy, TimingPair, evaluate
from optimizer_core.backend import DaytonaBackend, TrialRequest
from optimizer_core.config import DaytonaSettings
from optimizer_core.canonical import canonical_json, digest, parse_output
from optimizer_core.ledger import append_event, verify_ledger
from optimizer_core.receipts import audit_receipt, create_receipt

POLICY = AcceptancePolicy()
GATES = dict(correctness_passed=True, verifier_passed=True, evidence_complete=True)


def pairs():
    environment = digest(b"fixed test environment")
    return [TimingPair(digest(f"input-{i}".encode()), 1.0, 0.25,
                       digest(f"out-{i}".encode()), digest(f"out-{i}".encode()),
                       environment) for i in range(30)]


class OutputTests(unittest.TestCase):
    def test_key_order_canonicalization(self):
        self.assertEqual(parse_output(b'{"b":2,"a":[1,"x"]}'),
                         b'{"a":[1,"x"],"b":2}')

    def test_reject_ambiguous_or_wrong_types(self):
        for data in (b'{"x":1,"x":2}', b"true", b"null", b"1.0", b"NaN",
                     b"Infinity", b'{"x":false}', b'{"x":1} trailing',
                     b'"\xff"', b"[1,2"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_output(data)

    def test_output_byte_limit(self):
        with self.assertRaises(ValueError):
            parse_output(b'"large"', max_bytes=3)

    def test_deeply_nested_output_rejected(self):
        with self.assertRaises(ValueError):
            parse_output(b"[" * 2000 + b"1" + b"]" * 2000)


class AcceptanceTests(unittest.TestCase):
    def evaluate(self, trials=None, **overrides):
        arguments = dict(origin="measured", policy=POLICY, **GATES)
        arguments.update(overrides)
        return evaluate(pairs() if trials is None else trials, **arguments)

    def test_full_valid_evidence_can_meet_mathematical_gate(self):
        result = self.evaluate()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["ci_lower"], 4.0)

    def test_synthetic_can_never_be_accepted(self):
        result = self.evaluate(origin="synthetic")
        self.assertFalse(result["accepted"])
        self.assertIn("synthetic_evidence", result["reasons"])

    def test_screening_and_incomplete_data_cannot_accept(self):
        for count in (0, 5, 29, 31):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.evaluate((pairs() * 2)[:count])

    def test_each_required_gate_blocks_acceptance(self):
        for gate in GATES:
            with self.subTest(gate=gate):
                self.assertFalse(self.evaluate(**{gate: False})["accepted"])

    def test_timed_output_mismatch_blocks_acceptance(self):
        trials = pairs()
        trials[0] = replace(trials[0], candidate_output_sha256=digest(b"wrong"))
        self.assertIn("timed_output_mismatch", self.evaluate(trials)["reasons"])

    def test_bad_timings_duplicate_inputs_and_mixed_hosts_rejected(self):
        for replacement in (
            {"candidate_seconds": 0}, {"candidate_seconds": -1},
            {"candidate_seconds": float("nan")}, {"baseline_seconds": float("inf")},
            {"baseline_seconds": True}, {"input_sha256": pairs()[1].input_sha256},
            {"environment_sha256": digest(b"another host")},
        ):
            trials = pairs()
            trials[0] = replace(trials[0], **replacement)
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                self.evaluate(trials)

    def test_median_can_pass_while_lower_bound_fails(self):
        trials = [replace(item, baseline_seconds=2.0 if i < 14 else 4.0,
                          candidate_seconds=1.0) for i, item in enumerate(pairs())]
        result = self.evaluate(trials)
        self.assertGreaterEqual(result["median_speedup"], 3)
        self.assertFalse(result["accepted"])
        self.assertIn("ci_lower_bound_below_bar", result["reasons"])

    def test_bootstrap_is_reproducible(self):
        trials = [replace(item, baseline_seconds=2.5 + i / 10,
                          candidate_seconds=1.0) for i, item in enumerate(pairs())]
        self.assertEqual(self.evaluate(trials), self.evaluate(trials))

    def test_frozen_policy_cannot_be_weakened(self):
        for policy in (replace(POLICY, pairs=5),
                       replace(POLICY, minimum_speedup=1.0),
                       replace(POLICY, bootstrap_resamples=100),
                       replace(POLICY, ci_level=0.50)):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                policy.validate()


class ArtifactTests(unittest.TestCase):
    def test_ledger_detects_modified_event_or_incomplete_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            append_event(path, {"type": "plan", "origin": "synthetic"})
            append_event(path, {"type": "termination", "accepted": False})
            self.assertEqual(len(verify_ledger(path)), 2)
            original = path.read_bytes()
            path.write_bytes(original.replace(b'"accepted":false', b'"accepted":true'))
            with self.assertRaises(ValueError):
                verify_ledger(path)
            path.write_bytes(original[:-1])
            with self.assertRaises(ValueError):
                verify_ledger(path)

    def make_receipt(self, directory, *, origin="measured"):
        artifact = directory / "artifact.json"
        artifact.write_bytes(b'{"fixture":true}')
        receipt = create_receipt(
            pairs(), origin=origin, policy=POLICY, gates=GATES,
            artifacts=[{"path": artifact.name, "sha256": digest(artifact.read_bytes())}])
        path = directory / "receipt.json"
        path.write_bytes(canonical_json(receipt))
        return path

    def test_audit_requires_anchor_and_detects_receipt_or_artifact_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_receipt(Path(directory))
            anchor = digest(path.read_bytes())
            with self.assertRaises(ValueError):
                audit_receipt(path)
            self.assertTrue(audit_receipt(path, expected_sha256=anchor)["accepted"])
            with self.assertRaises(ValueError):
                audit_receipt(path, expected_sha256="0" * 64)
            (path.parent / "artifact.json").write_bytes(b"altered")
            with self.assertRaises(ValueError):
                audit_receipt(path, expected_sha256=anchor)

    def test_audit_rejects_artifact_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.make_receipt(root, origin="synthetic")
            import json
            receipt = json.loads(path.read_bytes())
            receipt["artifacts"][0]["path"] = "../outside.json"
            path.write_bytes(canonical_json(receipt))
            with self.assertRaises(ValueError):
                audit_receipt(path)

    def test_missing_key_never_falls_back_to_local_execution(self):
        request = TrialRequest("test", b"raise SystemExit()", b"{}",
                               1.0, digest(b"environment"))
        with self.assertRaises(ValueError):
            DaytonaBackend(DaytonaSettings(api_key="")).run_trial(request)


if __name__ == "__main__":
    unittest.main()
