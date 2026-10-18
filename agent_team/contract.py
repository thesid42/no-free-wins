"""Plain-JSON integration contract v1 with A's harness: requests, response checks, done test."""

import math
import re

from optimizer_core.canonical import digest

STATUSES = {"screened", "accepted", "simulated_acceptance", "rejected",
            "bar_not_met", "budget_exhausted", "infrastructure_error"}
RESPONSE_KEYS = {"schema_version", "request_id", "candidate_id", "status", "origin",
                 "accepted", "correctness_passed", "screen_median_speedup", "acceptance",
                 "receipt_id", "receipt_sha256", "evidence_ids", "feedback_public"}
FEEDBACK_KEYS = {"code", "summary", "evidence_ids"}
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def make_candidate(candidate_id, source):
    """Hash the exact transmitted UTF-8 bytes; CRLF/LF are preserved, never normalized."""
    return {"candidate_id": candidate_id, "source_utf8": source,
            "sha256": digest(source.encode("utf-8"))}


def build_request(*, request_id, session_id, candidate, mode, remaining_wall_seconds,
                  verifier=None, task_id="logs-parse-v1"):
    if mode not in ("screen", "acceptance"):
        raise ValueError("mode must be screen or acceptance")
    for value in (request_id, session_id):
        if type(value) is not str or not 1 <= len(value) <= 128:
            raise ValueError("invalid request/session identifier")
    if (type(candidate) is not dict or set(candidate) != {"candidate_id", "source_utf8", "sha256"}
            or type(candidate["candidate_id"]) is not str
            or not 1 <= len(candidate["candidate_id"]) <= 128
            or type(candidate["source_utf8"]) is not str
            or type(candidate["sha256"]) is not str
            or _SHA256.fullmatch(candidate["sha256"]) is None):
        raise ValueError("invalid candidate envelope")
    if not _number(remaining_wall_seconds) or remaining_wall_seconds <= 0:
        raise ValueError("remaining_wall_seconds must be positive and finite")
    if mode == "acceptance":
        if (type(verifier) is not dict or set(verifier) != {"approved", "evidence_ids"} or
                verifier["approved"] is not True or type(verifier["evidence_ids"]) is not list or
                not verifier["evidence_ids"] or
                not all(type(item) is str and item for item in verifier["evidence_ids"])):
            raise ValueError("acceptance requires an approved verifier with evidence ids")
    elif verifier is not None:
        raise ValueError("screen requests carry no verifier")
    return {"schema_version": 1, "request_id": request_id, "session_id": session_id,
            "task_id": task_id, "candidate": candidate, "mode": mode,
            "remaining_wall_seconds": remaining_wall_seconds, "verifier": verifier}


def validate_response(resp, request):
    """Raise ValueError unless resp is a well-formed contract v1 response for request."""
    if type(resp) is not dict or set(resp) != RESPONSE_KEYS:
        raise ValueError("response keys differ from contract v1")
    if type(resp["schema_version"]) is not int or resp["schema_version"] != 1:
        raise ValueError("unsupported response schema_version")
    if resp["request_id"] != request["request_id"]:
        raise ValueError("response request_id mismatch")
    if resp["candidate_id"] != request["candidate"]["candidate_id"]:
        raise ValueError("response candidate_id mismatch")
    if type(resp["status"]) is not str or resp["status"] not in STATUSES:
        raise ValueError("unknown response status")
    if type(resp["origin"]) is not str or resp["origin"] not in ("synthetic", "measured"):
        raise ValueError("unknown response origin")
    if type(resp["accepted"]) is not bool or type(resp["correctness_passed"]) is not bool:
        raise ValueError("accepted/correctness_passed must be booleans")
    if resp["screen_median_speedup"] is not None and not _number(resp["screen_median_speedup"]):
        raise ValueError("invalid screen_median_speedup")
    if resp["acceptance"] is not None and type(resp["acceptance"]) is not dict:
        raise ValueError("acceptance must be null or an object")
    for key in ("receipt_id", "receipt_sha256"):
        if resp[key] is not None and type(resp[key]) is not str:
            raise ValueError(f"{key} must be null or a string")
    if (type(resp["evidence_ids"]) is not list or
            not all(type(item) is str for item in resp["evidence_ids"])):
        raise ValueError("evidence_ids must be a list of strings")
    feedback = resp["feedback_public"]
    if type(feedback) is not list:
        raise ValueError("feedback_public must be a list")
    for item in feedback:
        if (type(item) is not dict or set(item) != FEEDBACK_KEYS or
                type(item["code"]) is not str or type(item["summary"]) is not str or
                type(item["evidence_ids"]) is not list or
                not all(type(e) is str for e in item["evidence_ids"])):
            raise ValueError("invalid feedback_public entry")
    return resp


def is_verified_done(resp):
    """True only for a measured, receipted acceptance whose median and CI lower bound are strictly above the policy bar (1.0, or legacy 3.0). Never raises."""
    try:
        acceptance = resp["acceptance"]
        policy = acceptance.get("policy") if type(acceptance) is dict else None
        policy_min = policy.get("minimum_speedup") if type(policy) is dict else None
        if not _number(policy_min) or policy_min not in (1.0, 3.0):
            policy_min = 1.0
        return (resp["status"] == "accepted" and resp["origin"] == "measured" and
                resp["accepted"] is True and resp["correctness_passed"] is True and
                type(resp["receipt_id"]) is str and bool(resp["receipt_id"]) and
                type(resp["receipt_sha256"]) is str and
                _SHA256.fullmatch(resp["receipt_sha256"]) is not None and
                type(acceptance) is dict and acceptance["accepted"] is True and
                acceptance.get("origin") == "measured" and acceptance.get("reasons") == [] and
                type(acceptance["pairs"]) is int and acceptance["pairs"] == 30 and
                _number(acceptance["median_speedup"]) and
                acceptance["median_speedup"] > policy_min and
                _number(acceptance["ci_lower"]) and acceptance["ci_lower"] > policy_min)
    except (KeyError, TypeError):
        return False
