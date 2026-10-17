from pathlib import Path
import tempfile
import unittest

from optimizer_core.backend import InfrastructureError, TrialResult
from optimizer_core.canonical import canonical_json, digest, strict_json
from optimizer_core.tasks import reference
from optimizer_core.zoo import run_zoo


class ZooBackend:
    environment_sha256 = digest(b"fixture-runtime")
    def __init__(self, *, corrupt=False, clean=True):
        self.corrupt, self.clean, self.closed = corrupt, clean, False
    def close(self):
        self.closed = True
        return self.clean
    def run_trial(self, request):
        cases = strict_json(request.input_bytes)["cases"]
        exit_code = 0
        if b"yield lines" in request.candidate_source:
            exit_code, wire = 1, b""
        elif b"raise SystemExit(0)" in request.candidate_source:
            wire = b""
        else:
            wrong = request.candidate_source == b"def solve(lines):\n    return {}\n"
            wire = canonical_json({"results": [{"id": item["id"],
                      "output": {} if wrong else reference(item["input"])} for item in cases]})
        proof = canonical_json({"origin": "synthetic", "trial": request.trial_id})
        return TrialResult(request.trial_id, wire, exit_code, 0.1,
                           self.environment_sha256, "fake-zoo",
                           "0" * 64 if self.corrupt else digest(proof), True,
                           proof_bytes=proof, origin="synthetic")


class ZooTests(unittest.TestCase):
    def test_control_report_is_explicitly_not_model_or_full_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = ZooBackend()
            result = run_zoo(backend, output=directory)
            self.assertEqual(result["attacks_caught"], 4)
            self.assertEqual(result["honest_rejected"], 0)
            self.assertFalse(result["model_verifier_evaluated"])
            self.assertTrue(backend.closed)
            self.assertTrue(Path(result["report_path"]).exists())

    def test_corrupt_proof_prevents_zoo_success_and_still_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = ZooBackend(corrupt=True)
            with self.assertRaises(InfrastructureError):
                run_zoo(backend, output=directory)
            self.assertTrue(backend.closed)

    def test_failed_machine_deletion_prevents_report(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(InfrastructureError):
                run_zoo(ZooBackend(clean=False), output=directory)
            self.assertEqual(list(Path(directory).glob("*/report.json")), [])
