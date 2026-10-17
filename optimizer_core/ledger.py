"""Single-controller append-only JSONL with a verifiable hash chain.

Integrity is relative to a separately sealed final digest, not a signature.
Never grant a candidate write access to this directory.
"""

import os
from pathlib import Path

from .canonical import canonical_json, digest, strict_json

ZERO = "0" * 64


def verify_ledger(path):
    path = Path(path)
    previous, records = ZERO, []
    with path.open("rb") as stream:
        for sequence, line in enumerate(stream, 1):
            if len(line) > 1_048_576 or not line.endswith(b"\n"):
                raise ValueError("oversized or incomplete ledger line")
            record = strict_json(line)
            if set(record) != {"seq", "prev_sha256", "event", "sha256"}:
                raise ValueError("invalid ledger record")
            if type(record["seq"]) is not int or record["seq"] != sequence:
                raise ValueError("ledger sequence mismatch")
            if record["prev_sha256"] != previous:
                raise ValueError("ledger chain mismatch")
            body = {key: record[key] for key in ("seq", "prev_sha256", "event")}
            if digest(canonical_json(body)) != record["sha256"]:
                raise ValueError("ledger digest mismatch")
            previous = record["sha256"]
            records.append(record)
    return records


def append_event(path, event):
    """Single writer only; the future orchestrator owns serialization."""
    if type(event) is not dict or not event.get("type"):
        raise ValueError("event requires a type")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = verify_ledger(path) if path.exists() else []
    body = {"seq": len(records) + 1,
            "prev_sha256": records[-1]["sha256"] if records else ZERO,
            "event": event}
    record = {**body, "sha256": digest(canonical_json(body))}
    data = canonical_json(record) + b"\n"
    if len(data) > 1_048_576:
        raise ValueError("event exceeds ledger limit")
    with path.open("ab") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return record
