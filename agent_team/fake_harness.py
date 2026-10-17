"""Fake evaluate_candidate for contract v1. It can never claim real acceptance.

Every response has origin=synthetic and accepted=False, whatever the script says.
A script maps candidate_id -> {"screen": {...}, "acceptance": {...}} overrides; an
Exception instance at either level is raised to simulate infrastructure failure.
"""

import copy
import math

from optimizer_core.canonical import canonical_json, digest

from .contract import validate_response

OVERRIDES = {"status", "correctness_passed", "screen_median_speedup", "feedback_public",
             "acceptance", "evidence_ids", "receipt_id", "receipt_sha256",
             "origin", "accepted"}  # origin/accepted are accepted but forced back
REQUEST_KEYS = {"schema_version", "request_id", "session_id", "task_id",
                "candidate", "mode", "remaining_wall_seconds", "verifier"}


class FakeHarness:
    def __init__(self, script=None, task_id="logs-parse-v1"):
        if type(task_id) is not str or not 1 <= len(task_id) <= 128:
            raise ValueError("task_id must be a 1-128 character string")
        self.script, self.calls, self._n = dict(script or {}), [], 0
        self.task_id = task_id

    def _validate(self, request):
        if (type(request) is not dict or set(request) != REQUEST_KEYS or
                type(request["schema_version"]) is not int or request["schema_version"] != 1 or
                request["task_id"] != self.task_id or
                request["mode"] not in ("screen", "acceptance")):
            raise ValueError("unsupported candidate-evaluation request")
        for key in ("request_id", "session_id"):
            if type(request[key]) is not str or not 1 <= len(request[key]) <= 128:
                raise ValueError("invalid request/session identifier")
        candidate = request["candidate"]
        if type(candidate) is not dict or set(candidate) != {"candidate_id", "source_utf8", "sha256"}:
            raise ValueError("invalid candidate envelope")
        if (type(candidate["candidate_id"]) is not str or
                not 1 <= len(candidate["candidate_id"]) <= 128 or
                type(candidate["source_utf8"]) is not str):
            raise ValueError("invalid candidate identity/source")
        source = candidate["source_utf8"].encode("utf-8")
        if len(source) > 262_144 or digest(source) != candidate["sha256"]:
            raise ValueError("candidate source hash/size mismatch")
        remaining = request["remaining_wall_seconds"]
        if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining <= 0:
            raise ValueError("invalid remaining wall budget")

    def evaluate_candidate(self, request):
        self._validate(request)
        self.calls.append(copy.deepcopy(request))
        mode, candidate_id = request["mode"], request["candidate"]["candidate_id"]
        self._n += 1
        evidence = [f"fixture-{mode}-{self._n:03d}"]
        spec = self.script.get(candidate_id, {})
        if isinstance(spec, Exception):
            raise spec
        overrides = spec.get(mode, {})
        if isinstance(overrides, Exception):
            raise overrides
        if set(overrides) - OVERRIDES:
            raise ValueError("unknown scripted override")
        verifier = request["verifier"]
        approved = (type(verifier) is dict and verifier.get("approved") is True
                    and type(verifier.get("evidence_ids")) is list
                    and bool(verifier["evidence_ids"])
                    and all(type(item) is str and item for item in verifier["evidence_ids"]))
        resp = {"schema_version": 1, "request_id": request["request_id"],
                "candidate_id": candidate_id, "status": "screened", "origin": "synthetic",
                "accepted": False, "correctness_passed": True, "screen_median_speedup": 1.0,
                "acceptance": None, "receipt_id": None, "receipt_sha256": None,
                "evidence_ids": evidence, "feedback_public": []}
        if mode == "acceptance" and not approved:
            resp.update(status="rejected", correctness_passed=False, screen_median_speedup=None,
                        feedback_public=[{"code": "verifier_evidence_required",
                                          "summary": "Acceptance needs an approved verifier verdict",
                                          "evidence_ids": evidence}])
            return validate_response(resp, request)
        if mode == "acceptance":
            resp.update(status="simulated_acceptance", screen_median_speedup=None,
                        acceptance={"accepted": False, "origin": "synthetic", "pairs": 30,
                                    "median_speedup": 4.0, "ci_lower": 3.5, "ci_upper": 4.5,
                                    "reasons": ["synthetic_evidence"],
                                    "policy": {"pairs": 30, "minimum_speedup": 3.0,
                                               "bootstrap_resamples": 10000,
                                               "bootstrap_seed": 42, "ci_level": 0.95}})
        for key, value in copy.deepcopy(overrides).items():
            if key == "acceptance" and resp["acceptance"] is not None and type(value) is dict:
                resp["acceptance"].update(value)
            elif key not in ("origin", "accepted"):
                resp[key] = value
        # The fake can never claim real acceptance, whatever the script asks for.
        resp["origin"], resp["accepted"] = "synthetic", False
        if resp["status"] == "accepted":
            resp["status"] = "simulated_acceptance"
        if type(resp["acceptance"]) is dict:
            resp["acceptance"].update(accepted=False, origin="synthetic")
        if (resp["status"] == "simulated_acceptance" and type(resp["acceptance"]) is dict
                and "receipt_id" not in overrides and "receipt_sha256" not in overrides):
            resp["receipt_id"] = f"fixture-receipt-{self._n:03d}"
            resp["receipt_sha256"] = digest(canonical_json(resp["acceptance"]))
        return validate_response(resp, request)
