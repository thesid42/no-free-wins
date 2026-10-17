import argparse
import json
from pathlib import Path
import sys

from .acceptance import TimingPair
from .canonical import canonical_json, digest, strict_json
from .ledger import append_event, verify_ledger
from .receipts import audit_receipt, create_receipt


def demo(output):
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / "synthetic_fixture.json"
    if (output / "receipt.json").exists() or artifact.exists():
        raise ValueError("demo output already exists; choose a fresh directory")
    artifact.write_bytes(canonical_json({"origin": "synthetic", "code_executed": False}))
    environment = digest(b"synthetic environment, not a measured Linux runner")
    pairs = [TimingPair(digest(f"input-{i}".encode()), 1.0, 0.25,
                        digest(f"output-{i}".encode()), digest(f"output-{i}".encode()),
                        environment) for i in range(30)]
    receipt = create_receipt(
        pairs, origin="synthetic",
        gates={"correctness_passed": True, "verifier_passed": True,
               "evidence_complete": False},
        artifacts=[{"path": artifact.name, "sha256": digest(artifact.read_bytes())}])
    receipt_path = output / "receipt.json"
    receipt_path.write_bytes(canonical_json(receipt))
    ledger_path = output / "ledger.jsonl"
    append_event(ledger_path, {"type": "termination", "origin": "synthetic",
                              "receipt_sha256": digest(receipt_path.read_bytes()),
                              "accepted": False})
    decision = audit_receipt(receipt_path)
    verify_ledger(ledger_path)
    return {"mode": "synthetic_offline", "audit_valid": True,
            "accepted": decision["accepted"],
            "simulated_median_speedup": decision["median_speedup"],
            "receipt": str(receipt_path.resolve())}


def load_proposal_file(path, *, project=None, repos=None):
    """Read a prepare-repo proposal file; returns (repo, root, origin_info, proposal).

    The root was fetched by the analyst, so it is only checked to exist and to sit inside the
    project or private/repos (no re-clone, nothing is executed).
    """
    try:
        raw = strict_json(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError(f"proposal file is not readable strict JSON: {exc}") from None
    if type(raw) is not dict:
        raise ValueError("proposal file must be a JSON object")
    unknown = set(raw) - {"repo", "root", "origin_info", "proposal"}
    missing = {"root", "proposal"} - set(raw)
    if unknown or missing:
        raise ValueError(f"proposal file keys: unknown {sorted(unknown)}, missing {sorted(missing)}")
    if type(raw["root"]) is not str or not raw["root"]:
        raise ValueError("proposal file root must be a path string")
    root = Path(raw["root"]).resolve()
    project = Path(project or Path(__file__).resolve().parents[1]).resolve()
    allowed = [project, (project / (repos or "private/repos")).resolve()]
    if not root.is_dir():
        raise ValueError(f"proposal root {raw['root']} is not a directory")
    if not any(root.is_relative_to(base) for base in allowed):
        raise ValueError("proposal root must be inside the project or private/repos")
    if "repo" in raw and type(raw["repo"]) is not str:
        raise ValueError("proposal file repo must be a string")
    origin_info = raw.get("origin_info")
    if origin_info is None:
        origin_info = {"kind": "local", "path": str(root)}
    if type(origin_info) is not dict:
        raise ValueError("proposal file origin_info must be an object")
    if type(raw["proposal"]) is not dict:
        raise ValueError("proposal file proposal must be an object")
    return raw.get("repo", str(root)), root, origin_info, raw["proposal"]


def build_parser():
    parser = argparse.ArgumentParser(description="Disclosed pre-event foundations")
    commands = parser.add_subparsers(dest="command", required=True)
    simulate = commands.add_parser("demo", help="synthetic only; no code/API execution")
    simulate.add_argument("--output", type=Path, required=True)
    audit = commands.add_parser("audit", help="recompute a recorded receipt decision")
    audit.add_argument("receipt", type=Path)
    audit.add_argument("--sha256", help="receipt digest from the sealed controller ledger")
    commands.add_parser("doctor", help="check local setup without printing secrets or making API calls")
    commands.add_parser("probe", help="live security/timeout checks; deletes the disposable machine")
    commands.add_parser("cleanup", help="reconcile only sandboxes recorded by this controller")
    commands.add_parser("zoo", help="live deterministic development fixtures, no model-verifier claim")
    prepare = commands.add_parser("prepare", help="commit private seeds before starting agents")
    prepare.add_argument("--session-id", required=True)
    prepare.add_argument("--lines", type=int, default=100_000)
    prepare_repo = commands.add_parser(
        "prepare-repo", help="seal a repo task (local path or GitHub URL) in the sandbox")
    prepare_repo.add_argument("--session-id", required=True)
    source = prepare_repo.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="local directory or GitHub URL with a nofreewins.json")
    source.add_argument("--proposal", type=Path,
                        help="analyst proposal JSON ({repo, root, origin_info, proposal})")
    prepare_repo.add_argument("--include", choices=("package", "full"),
                              help="override the include mode in nofreewins.json")
    evaluate_cmd = commands.add_parser("evaluate", help="B-compatible JSON evaluation")
    evaluate_cmd.add_argument("--request", type=Path)
    evaluate_cmd.add_argument("--reference", action="store_true",
                              help="evaluate the known reference as a development calibration")
    evaluate_cmd.add_argument("--session-id")
    evaluate_cmd.add_argument("--mode", choices=("screen", "acceptance"), default="acceptance")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "demo":
        result = demo(args.output)
    elif args.command == "audit":
        result = {"audit_valid": True,
                  "decision": audit_receipt(args.receipt, expected_sha256=args.sha256)}
    elif args.command == "doctor":
        from .config import DaytonaSettings
        from importlib.metadata import PackageNotFoundError, version
        result = DaytonaSettings.load().public_status()
        try:
            result["daytona_sdk"] = version("daytona")
        except PackageNotFoundError:
            result["daytona_sdk"] = "not installed"
    elif args.command in ("probe", "cleanup", "zoo"):
        from .backend import DaytonaBackend, verified_backend
        if args.command == "cleanup":
            with DaytonaBackend() as backend:
                result = backend.reconcile()
        else:
            with verified_backend() as backend:
                result = backend.probe_report
                if args.command == "zoo":
                    from .zoo import run_zoo
                    result = run_zoo(backend)
        if args.command == "probe":
            result["sandbox_deleted"] = backend.closed and backend.sandbox is None
            Path("artifacts/daytona_probe.json").write_bytes(canonical_json(result))
    elif args.command == "prepare":
        from .sessions import prepare_session
        result = prepare_session(args.session_id, workload_lines=args.lines)
        directory = Path("artifacts/commitments")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (args.session_id + ".json")).write_bytes(canonical_json(result))
    elif args.command == "prepare-repo":
        from .backend import verified_backend
        from .repo_task import prepare_repo_session
        if args.proposal:
            _, root, origin_info, proposal = load_proposal_file(args.proposal)
            fields = dict(proposal=proposal, origin_info=origin_info)
        else:
            root, fields = args.repo, {}
        with verified_backend() as backend:
            result = prepare_repo_session(args.session_id, str(root), backend=backend,
                                          include=args.include, **fields)
        directory = Path("artifacts/commitments")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (args.session_id + ".json")).write_bytes(canonical_json(result))
    else:
        from .harness import evaluate_candidate
        from .tasks import REFERENCE_SOURCE
        if args.reference:
            if not args.session_id or args.request:
                parser.error("--reference needs --session-id and cannot combine with --request")
            request = {"schema_version": 1, "request_id": "reference-calibration",
                       "session_id": args.session_id, "task_id": "logs-parse-v1",
                       "candidate": {"candidate_id": "trusted-reference",
                                     "source_utf8": REFERENCE_SOURCE.decode(),
                                     "sha256": digest(REFERENCE_SOURCE)},
                       "mode": args.mode, "remaining_wall_seconds": 900,
                       "verifier": {"approved": True, "evidence_ids": [
                           "pre-event-known-reference:" + digest(REFERENCE_SOURCE)]}}
        elif args.request:
            request = strict_json(args.request.read_bytes())
        else:
            request = strict_json(sys.stdin.buffer.read())
        result = evaluate_candidate(request)
        if args.reference:
            result["phase"] = "development"
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        # SDK failures are sanitized by the backend; do not print configuration.
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
