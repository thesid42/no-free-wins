"""Read-only run inspector for the Sidecar UI server.

Usage: inspect_run.py <runs_root> <runId> [--artifact original|optimized]

All hashing and ledger/receipt verification is delegated to optimizer_core so the
Node server never reimplements canonical JSON or hash chains. Prints exactly one JSON
object. Never executes repo or candidate code and never writes anything.
"""

import base64
import json
import os
from pathlib import Path
import re
import sys
import zlib

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from optimizer_core.canonical import canonical_json, digest, strict_json  # noqa: E402
from optimizer_core.ledger import verify_ledger  # noqa: E402
from optimizer_core.receipts import audit_receipt  # noqa: E402

RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


PATHLIKE = re.compile(r"(?:[A-Za-z]:\\|/)[^\s'\"]+")


def _safe_error(exc):
    """Error text with no absolute paths: OSError reports only its strerror, others are scrubbed."""
    if isinstance(exc, OSError):
        text = exc.strerror or "I/O error"
    else:
        text = PATHLIKE.sub("<path>", str(exc))
    return f"{type(exc).__name__}: {text[:160]}"


def _safe_child(directory, name):
    """Resolve name as a plain file inside directory, or None."""
    if not SAFE_ID.match(name or "") or ".." in name:
        return None
    base = directory.resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        return None
    return target


def _terminal(events):
    return next((e for e in reversed(events) if e.get("type") == "terminal"), None)


def _accepted(events):
    return next((e for e in reversed(events)
                 if e.get("type") == "acceptance_result" and e.get("accepted") is True), None)


def _is_repo(events):
    return any(str(e.get("task_id", "")).startswith("repo-") for e in events)


def _session_id(events):
    started = next((e for e in events if e.get("type") == "run_started"), None)
    return started.get("session_id") if started else None


SEALED_FILES = ("spec.json", "bundle.b64", "cases.json", "expected.json", "workloads.json", "public.json")
BUNDLE_LIMIT = (8 << 20) + (1 << 20)


def _sealed_repo_original(session_id):
    """Read a repo run's target file straight from its sealed session; never imports or runs repo code.

    Deliberately avoids load_session/RepoTask.load: their generator-hash check rejects completed
    sessions once repo_task.py is edited, which would make verified runs unreplayable. The sealed
    files are still verified against the digests recorded in the session itself.
    """
    sessions = Path(os.environ.get("NFW_SESSIONS_ROOT") or REPO_ROOT / "private" / "sessions")
    if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
        raise ValueError("invalid session id")
    session = strict_json((sessions / session_id / "session.json").read_bytes())
    repo = session.get("repo")
    if session.get("session_id") != session_id or not isinstance(repo, dict) \
            or not isinstance(repo.get("files"), dict) or set(repo["files"]) != set(SEALED_FILES):
        raise ValueError("session has no sealed repo task")
    sealed = {}
    for name in SEALED_FILES:
        data = (sessions / f"{session_id}.repo" / name).read_bytes()
        if digest(data) != repo["files"][name]:
            raise ValueError(f"sealed repo file {name} changed")
        sealed[name] = data
    if digest(sealed["spec.json"]) != repo.get("spec_sha256"):
        raise ValueError("sealed spec does not match the session")
    inflater = zlib.decompressobj()
    raw = inflater.decompress(base64.b64decode(sealed["bundle.b64"], validate=True), BUNDLE_LIMIT)
    if inflater.unconsumed_tail:
        raise ValueError("sealed bundle decompresses past its size limit")
    sources = strict_json(raw)
    if not isinstance(sources, dict) or digest(canonical_json(sources)) != repo.get("bundle_sha256"):
        raise ValueError("sealed bundle does not match the session")
    path = strict_json(sealed["spec.json"])["target"].rpartition(":")[0]
    if path not in sources or not isinstance(sources[path], str):
        raise ValueError("target file is not in the sealed bundle")
    current = digest((REPO_ROOT / "optimizer_core" / "repo_task.py").read_bytes())
    return path.rsplit("/", 1)[-1], sources[path].encode("utf-8"), current == session.get("generator_sha256")


def _original(events):
    """Return (kind, filename, bytes, generatorHashCurrent|None) for the baseline source; read-only."""
    if not _is_repo(events):
        from optimizer_core.tasks import BASELINE_SOURCE
        return "logs", "baseline.py", BASELINE_SOURCE, None
    filename, data, current = _sealed_repo_original(_session_id(events))
    return "repo", filename, data, current


def _optimized(events, directory):
    """Return (candidate_id, filename, bytes, sha256) after binding to the ledger digest."""
    accepted, terminal = _accepted(events), _terminal(events)
    if accepted:
        cid, sha = accepted.get("candidate_id"), accepted.get("candidate_sha256")
    elif terminal and terminal.get("last_forwarded_candidate_id"):
        cid, sha = terminal["last_forwarded_candidate_id"], terminal.get("last_forwarded_candidate_sha256")
    else:
        raise LookupError("no accepted or forwarded candidate")
    target = _safe_child(directory / "candidates", f"{cid}.py")
    if target is None:
        raise LookupError("candidate file missing")
    data = target.read_bytes()
    actual = digest(data)
    if not isinstance(sha, str) or not SHA256.match(sha) or actual != sha:
        raise ValueError("candidate file does not match the ledger digest")
    return cid, target.name, data, actual


def _audit(events, receipts_root):
    """Audit the receipt of the accepted acceptance event (else the last one that names a receipt)."""
    event = (next((e for e in reversed(events) if e.get("type") == "acceptance_result"
                   and e.get("accepted") is True and e.get("receipt_id")), None)
             or next((e for e in reversed(events) if e.get("type") == "acceptance_result"
                      and e.get("receipt_id")), None))
    if event is None:
        return None, None
    receipt_id, sha = event["receipt_id"], event.get("receipt_sha256")
    if not isinstance(receipt_id, str) or not RUN_ID.match(receipt_id) or ".." in receipt_id:
        return {"valid": False, "error": "invalid receipt id"}, None
    try:
        decision = audit_receipt(receipts_root / receipt_id / "receipt.json", expected_sha256=sha)
        return {"valid": True, "decision": decision}, receipt_id
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"valid": False, "error": _safe_error(exc)}, receipt_id


def main(argv):
    args = argv[1:]
    artifact = None
    if "--artifact" in args:
        position = args.index("--artifact")
        artifact = args[position + 1] if position + 1 < len(args) else None
        args = args[:position] + args[position + 2:]
        if artifact not in ("original", "optimized"):
            return {"error": "bad_artifact"}
    if len(args) != 2:
        return {"error": "usage"}
    runs_root, run_id = Path(args[0]).resolve(), args[1]
    if not RUN_ID.match(run_id) or ".." in run_id:
        return {"error": "bad_run_id"}
    directory = (runs_root / run_id).resolve()
    ledger = directory / "ledger.jsonl"
    if not directory.is_relative_to(runs_root) or not ledger.is_file():
        return {"error": "not_found"}
    try:
        records = verify_ledger(ledger)
        events = [record["event"] for record in records]
        verified, error = True, None
        final = records[-1]["sha256"] if records else None
    except (OSError, ValueError, KeyError) as exc:
        events, verified, final = [], False, None
        error = _safe_error(exc)
        if artifact:
            return {"error": "ledger_invalid", "detail": error}
    if artifact == "original":
        try:
            kind, filename, data, current = _original(events)
        except (OSError, ValueError, KeyError, LookupError) as exc:
            return {"error": "unavailable", "detail": _safe_error(exc)}
        return {"kind": kind, "filename": filename, "sha256": digest(data),
                "generatorHashCurrent": current, "base64": base64.b64encode(data).decode("ascii")}
    if artifact == "optimized":
        try:
            cid, filename, data, sha = _optimized(events, directory)
        except LookupError:
            return {"error": "not_found"}
        except ValueError as exc:
            return {"error": "hash_mismatch", "detail": str(exc)}
        return {"candidateId": cid, "filename": filename, "sha256": sha,
                "base64": base64.b64encode(data).decode("ascii")}
    candidates = {}
    for event in events:
        if event.get("type") == "candidate_created":
            target = _safe_child(directory / "candidates", f"{event.get('candidate_id')}.py")
            if target is not None:
                candidates[event["candidate_id"]] = digest(target.read_bytes())
    original = None
    try:
        kind, filename, data, current = _original(events)
        original = {"kind": kind, "filename": filename, "sha256": digest(data),
                    "generatorHashCurrent": current}
    except (OSError, ValueError, KeyError, LookupError) as exc:
        original = {"kind": "repo" if _is_repo(events) else "logs", "filename": None,
                    "sha256": None, "generatorHashCurrent": None, "error": _safe_error(exc)}
    audit, receipt_id = _audit(events, runs_root.parent / "evaluations") if verified else (None, None)
    result = {"ledgerVerified": verified, "ledgerFinalSha256": final, "events": events,
              "receiptAudit": audit, "receiptId": receipt_id,
              "candidates": candidates, "original": original}
    if error:
        result["error"] = error
    return result


if __name__ == "__main__":
    try:
        output = main(sys.argv)
    except Exception as exc:  # never crash without a JSON object
        output = {"error": "internal", "detail": _safe_error(exc)}
    sys.stdout.write(json.dumps(output))
