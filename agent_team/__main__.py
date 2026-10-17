"""CLI: python -m agent_team simulate. Fully synthetic; never a verified result."""

import argparse
import json
import math
from pathlib import Path
import sys

from optimizer_core.ledger import verify_ledger

from . import contract
from .budget import Budget
from .calls import ModelOrder
from .crusoe import CrusoeClient, price_per_1k, resolve_model
from .fake_harness import FakeHarness
from .models import FixtureModelClient, ModelError
from .orchestrator import Orchestrator

SOURCE_A = '''import re

_LINE = re.compile(r"(DEBUG|INFO|WARN|ERROR) user=([A-Za-z0-9_-]{1,32}) latency=([0-9]{1,7})")


def solve(lines):
    by_level = {"DEBUG": 0, "INFO": 0, "WARN": 0, "ERROR": 0}
    by_user = {}
    match = _LINE.fullmatch
    for line in lines:
        found = match(line)
        if found is None:
            continue
        level, user, latency = found.groups()
        by_level[level] += 1
        entry = by_user.get(user)
        if entry is None:
            by_user[user] = {"count": 1, "latency_sum": int(latency)}
        else:
            entry["count"] += 1
            entry["latency_sum"] += int(latency)
    return {"by_level": by_level, "by_user": by_user}
'''

SOURCE_B = '''import re

_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
_LINE = re.compile(r"(DEBUG|INFO|WARN|ERROR) user=([A-Za-z0-9_-]{1,32}) latency=([0-9]{1,7})")


def solve(lines):
    counts, latency_sums = {}, {}
    by_level = {level: 0 for level in _LEVELS}
    for line in lines:
        found = _LINE.fullmatch(line)
        if found:
            level, user, latency = found.groups()
            by_level[level] += 1
            counts[user] = counts.get(user, 0) + 1
            latency_sums[user] = latency_sums.get(user, 0) + int(latency)
    return {"by_level": by_level,
            "by_user": {u: {"count": counts[u], "latency_sum": latency_sums[u]} for u in counts}}
'''

SIMULATION_LABEL = "synthetic simulation; never a verified result"
PLAN = json.dumps({"strategies": [
    {"strategy_id": "single-pass-regex",
     "description": "Parse each line once with a compiled regex and aggregate in the same pass."},
    {"strategy_id": "split-and-dict",
     "description": "Validate fields by splitting lines and aggregate with plain dictionaries."}]})


def _fence(source):
    return f"```python\n{source}```"


def build_simulation(inject_failures=False):
    """Return (client, harness, orders, annotations, candidate sources) for the synthetic run."""
    sha_a = contract.make_candidate("worker_a-r1", SOURCE_A)["sha256"]
    verdict = json.dumps({"approved": True, "candidate_sha256": sha_a,
                          "rationale": "Single-pass regex is honest and general."})
    worker_a = [_fence(SOURCE_A)]
    annotations = []
    if inject_failures:
        worker_a = [ModelError("injected fault: primary attempt"),
                    ModelError("injected fault: bounded retry")]
        annotations.append({"type": "fault_injection",
                            "label": "injected primary+retry failure for worker_a"})
    annotations.append({"type": "simulation_label", "label": SIMULATION_LABEL})
    client = FixtureModelClient({
        "fixture/planner-primary": [PLAN],
        "fixture/worker-primary": worker_a,
        "fixture/worker-fallback": [_fence(SOURCE_A)] if inject_failures else [],
        "fixture/worker-b-primary": [_fence(SOURCE_B)],
        "fixture/verifier-primary": [verdict]})
    harness = FakeHarness({
        "worker_a-r1": {"screen": {"screen_median_speedup": 4.0},
                        "acceptance": {"acceptance": {"median_speedup": 3.6, "ci_lower": 3.3}}},
        "worker_b-r1": {"screen": {"screen_median_speedup": 2.0}}})
    orders = {"planner": ModelOrder("fixture/planner-primary", "fixture/planner-fallback"),
              "worker_a": ModelOrder("fixture/worker-primary", "fixture/worker-fallback"),
              "worker_b": ModelOrder("fixture/worker-b-primary", "fixture/worker-b-fallback"),
              "verifier": ModelOrder("fixture/verifier-primary", "fixture/verifier-fallback")}
    return client, harness, orders, annotations, {"worker_a": SOURCE_A, "worker_b": SOURCE_B}


def simulate(output, *, inject_failures=False, deadline=900):
    client, harness, orders, annotations, _ = build_simulation(inject_failures)
    ledger = output / "ledger.jsonl"
    result = Orchestrator(
        evaluate=harness.evaluate_candidate, client=client, budget=Budget(), ledger_path=ledger,
        session_id="simulation-session", orders=orders, deadline_seconds=deadline,
        annotations=annotations,
        # Floors suited to the fake harness. Real-harness runs must keep the orchestrator defaults.
        min_harness_seconds={"screen": 1, "acceptance": 1}).run()
    records = verify_ledger(ledger)
    forwarded = result.events[-1]["last_forwarded_candidate_id"]
    return result, {
        "run_id": result.run_id, "state": result.state, "rounds": result.rounds,
        "ledger_path": result.ledger_path, "ledger_final_sha256": result.ledger_final_sha256,
        "ledger_verified": bool(records) and records[-1]["sha256"] == result.ledger_final_sha256,
        "budget": result.budget, "last_forwarded_candidate_id": forwarded,
        "note": "synthetic simulation; fake harness origin is always synthetic, not a verified result"}


def list_models(resolve_names):
    """Live /models listing with conservative prices; resolved maps NAME -> id or error text."""
    client = CrusoeClient.from_env()
    ids = client.list_models()
    resolved = {}
    for name in resolve_names:
        try:
            resolved[name] = resolve_model(name, ids)
        except ValueError as exc:
            resolved[name] = f"error: {exc}"
    return {"base_url": client.base_url,
            "models": [{"id": i, "usd_per_1k_conservative": price_per_1k(i)} for i in ids],
            "resolved": resolved}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent_team", description="B orchestration, fixtures only")
    commands = parser.add_subparsers(dest="command", required=True)
    sim = commands.add_parser(
        "simulate", help="synthetic simulation with fixture models and fake harness; "
                         "never a verified result")
    sim.add_argument("--output", type=Path, required=True, help="fresh or empty directory")
    sim.add_argument("--inject-failures", action="store_true",
                     help="labeled drill: worker_a primary and retry fail, fallback recovers")
    sim.add_argument("--deadline", type=float, default=900, help="wall deadline in seconds")
    mod = commands.add_parser("models", help="list the models the Crusoe key can see (live call)")
    mod.add_argument("--resolve", action="append", default=[], metavar="NAME",
                     help="resolve NAME against the live listing (repeatable)")
    args = parser.parse_args(argv)
    if args.command == "models":
        try:
            print(json.dumps(list_models(args.resolve), indent=2))
        except (ModelError, ValueError) as exc:
            print(json.dumps({"error": str(exc)[:300]}), file=sys.stderr)
            return 1
        return 0
    if not math.isfinite(args.deadline) or not 0 < args.deadline <= 1800:
        print("--deadline must be greater than 0 and at most 1800 seconds", file=sys.stderr)
        return 2
    output = args.output
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        print("output directory exists and is not empty", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)
    result, summary = simulate(output, inject_failures=args.inject_failures, deadline=args.deadline)
    print(json.dumps(summary, indent=2))
    return 0 if result.state == "simulation_completed" else 1


if __name__ == "__main__":
    sys.exit(main())
