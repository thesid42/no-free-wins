"""Offline receipt audit; this is not a fresh sandbox execution."""

from dataclasses import asdict
from pathlib import Path

from .acceptance import AcceptancePolicy, TimingPair, evaluate
from .canonical import canonical_json, digest, strict_json


def create_receipt(pairs, *, origin, gates, artifacts, policy=None):
    policy = policy or AcceptancePolicy()
    decision = evaluate(pairs, origin=origin, policy=policy, **gates)
    return {"schema_version": 1, "origin": origin, "policy": asdict(policy),
            "gates": gates, "pairs": [asdict(pair) for pair in pairs],
            "artifacts": artifacts, "decision": decision}


def audit_receipt(path, *, expected_sha256=None):
    path = Path(path).resolve()
    data = path.read_bytes()
    if expected_sha256 is not None and digest(data) != expected_sha256:
        raise ValueError("receipt does not match its sealed external digest")
    receipt = strict_json(data)
    if receipt["schema_version"] != 1:
        raise ValueError("unsupported receipt version")
    if receipt["origin"] == "measured" and expected_sha256 is None:
        raise ValueError("measured receipts require a sealed external digest")
    if not receipt["artifacts"]:
        raise ValueError("receipt has no artifacts")
    for artifact in receipt["artifacts"]:
        target = (path.parent / artifact["path"]).resolve()
        if not target.is_relative_to(path.parent) or not target.is_file():
            raise ValueError("artifact is missing or outside receipt directory")
        if digest(target.read_bytes()) != artifact["sha256"]:
            raise ValueError("artifact digest mismatch")
    policy = AcceptancePolicy(**receipt["policy"])
    pairs = [TimingPair(**pair) for pair in receipt["pairs"]]
    decision = evaluate(pairs, origin=receipt["origin"], policy=policy,
                        **receipt["gates"])
    if canonical_json(decision) != canonical_json(receipt["decision"]):
        raise ValueError("recorded decision does not reproduce")
    return decision
