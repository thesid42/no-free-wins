"""CLI: python -m agent_team simulate (fully synthetic, never a verified result), models, run,
analyze (the repo analyst) and approve (prepare the proposal, optionally start the live run)."""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

from optimizer_core.ledger import verify_ledger

from . import analyst, contract, live
from . import config as nfw_config
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


LIVE_NOTE = ("development live run; verified_done only from a measured, receipted "
             "harness acceptance")


def _error(exc):
    return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def run_live(args):
    """Full live run (real Crusoe models, real harness). Exit 0 only on verified_done."""
    orchestrator, info = live.build_live_run(
        session_id=args.session_id, output_dir=args.output, deadline_seconds=args.deadline,
        max_rounds=args.max_rounds, max_cost_usd=args.max_cost_usd, max_tokens=args.max_tokens,
        inject_failures=args.inject_failures)
    result = orchestrator.run()
    records = verify_ledger(result.ledger_path)
    summary = {
        "run_id": result.run_id, "state": result.state, "rounds": result.rounds,
        "ledger_path": result.ledger_path, "ledger_final_sha256": result.ledger_final_sha256,
        "ledger_verified": bool(records) and records[-1]["sha256"] == result.ledger_final_sha256,
        "budget": result.budget,
        "last_forwarded_candidate_id": result.events[-1]["last_forwarded_candidate_id"],
        "models": info["models"], "note": LIVE_NOTE}
    if "drill" in info:
        summary["drill"] = {**info["drill"], "injected_total": orchestrator.client.injected_total}
    return result, summary


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
    run = commands.add_parser(
        "run", help="live run: Crusoe models, priced budget, real harness (spends money; "
                    "use --check first)")
    run.add_argument("--session-id", required=True, help="prepared session id")
    run.add_argument("--output", type=Path, default=None,
                     help="fresh or empty directory (default artifacts/runs/<session>-<UTC time>)")
    run.add_argument("--deadline", type=float, default=None,
                     help="wall deadline in seconds (max 1800; default: run.deadline_seconds of the config)")
    run.add_argument("--max-rounds", type=int, default=None,
                     help="default: run.max_rounds of the config")
    run.add_argument("--max-cost-usd", type=float, default=None,
                     help="inference cost cap (max 25; default: run.max_cost_usd of the config)")
    run.add_argument("--max-tokens", type=int, default=None,
                     help="max tokens per model call (default: run.max_tokens_per_call of the config)")
    run.add_argument("--inject-failures", action="store_true",
                     help="labeled drill: worker_a's primary model fails twice before any request, "
                          "the fallback must recover")
    run.add_argument("--check", action="store_true",
                     help="preflight, list models, resolve lineup and budget; no inference, no sandbox")
    ana = commands.add_parser(
        "analyze", help="analyst: read a repo and a request, then propose, ask questions or decline "
                        "(spends a little on inference; no sandbox)")
    ana.add_argument("--repo", default=None, help="local path or public GitHub URL (new analysis)")
    ana.add_argument("--prompt", default=None, help="what to optimize, in plain words (new analysis)")
    ana.add_argument("--proposal-id", default=None, help="continue this analysis")
    ana.add_argument("--answer", default=None, help="your answer or feedback (with --proposal-id)")
    app = commands.add_parser(
        "approve", help="prepare an approved proposal in the sandbox; --run then starts the live run")
    app.add_argument("--proposal-id", required=True)
    app.add_argument("--session-id", required=True, help="new session id to prepare")
    app.add_argument("--run", action="store_true",
                     help="after a successful prepare, start the live run with the config defaults")
    args = parser.parse_args(argv)
    if args.command == "run":
        return main_run(args)
    if args.command == "analyze":
        return main_analyze(args)
    if args.command == "approve":
        return main_approve(args)
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


def main_run(args):
    try:
        run_defaults = nfw_config.load_config()["run"]
    except nfw_config.ConfigError as exc:
        print(json.dumps(_error(exc)), file=sys.stderr)
        return 1
    for name, key in (("deadline", "deadline_seconds"), ("max_rounds", "max_rounds"),
                      ("max_cost_usd", "max_cost_usd"), ("max_tokens", "max_tokens_per_call")):
        if getattr(args, name) is None:  # unset flags take the global config value
            setattr(args, name, run_defaults[key])
    try:
        live.check_session_id(args.session_id)
        live.check_deadline(args.deadline)
        live.check_cost(args.max_cost_usd)
        live.check_max_tokens(args.max_tokens)
        if args.max_rounds < 1:
            raise ValueError("--max-rounds must be positive")
        if args.output is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            args.output = Path("artifacts/runs") / f"{args.session_id}-{stamp}"
        if not args.check:
            live.check_output_dir(args.output)
    except ValueError as exc:
        print(str(exc)[:300], file=sys.stderr)
        return 2
    try:
        if args.check:
            info = live.check_live(
                args.session_id, deadline_seconds=args.deadline, max_rounds=args.max_rounds,
                max_cost_usd=args.max_cost_usd, max_tokens=args.max_tokens,
                inject_failures=args.inject_failures)
            print(json.dumps({"check": "ok", **info}, indent=2))
            return 0
        result, summary = run_live(args)
    except Exception as exc:  # one-line error, never a traceback (e.g. a corrupt session file)
        failure = _error(exc)
        print(json.dumps({"check": "failed", **failure} if args.check else failure), file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if result.state == "verified_done" else 1


def _usage_error(message):
    print(message[:300], file=sys.stderr)
    return 2


def _summary_lines(result):
    status, lines = result["status"], []
    if status in ("questions", "needs_input"):
        lines.append("The analyst has questions:" if status == "questions"
                     else "The analyst needs your input:")
        lines += [f"  {n}. {question}" for n, question in enumerate(result["questions"], 1)]
        if result.get("candidates"):
            lines.append("Candidate targets: " + ", ".join(result["candidates"]))
        lines.append(f'Answer with: python -m agent_team analyze --proposal-id '
                     f'{result["proposal_id"]} --answer "..."')
    elif status == "proposal":
        proposal, sizes = result["proposal"], result["proposal"]["sizes"]
        lines.append(f"Proposal {result['proposal_id']}: optimize {proposal['target']}")
        if proposal["description"]:
            lines.append(f"  {proposal['description']}")
        lines.append(f"  generator: {len(proposal['generator_source'].encode('utf-8'))} bytes, "
                     f"timing size {sizes['timing_size']}, include {proposal['include']}")
        lines += [f"  idea: {hint}" for hint in proposal["hints"]]
        lines += [f"  risk: {risk}" for risk in proposal["risks"]]
        lines.append(f"Approve (sandboxed prepare, then the run): python -m agent_team approve "
                     f"--proposal-id {result['proposal_id']} --session-id <SESSION> --run")
        lines.append(f'Or adjust: python -m agent_team analyze --proposal-id '
                     f'{result["proposal_id"]} --answer "..."')
    else:
        lines.append(f"Not feasible: {result['reason']}")
        lines += [f"  suggestion: {item}" for item in result["suggestions"]]
        lines.append(f'Reply to try again: python -m agent_team analyze --proposal-id '
                     f'{result["proposal_id"]} --answer "..."')
    return lines


def main_analyze(args):
    if args.proposal_id is None:
        if args.repo is None or args.prompt is None or args.answer is not None:
            return _usage_error("analyze needs --repo and --prompt, or --proposal-id and --answer")
    elif args.repo is not None or args.prompt is not None or args.answer is None:
        return _usage_error("analyze needs --repo and --prompt, or --proposal-id and --answer")
    try:
        if args.proposal_id is not None:
            analyst.check_id(args.proposal_id)
            if not args.answer.strip() or len(args.answer) > analyst.MAX_ANSWER_CHARS:
                raise ValueError(f"--answer must be 1-{analyst.MAX_ANSWER_CHARS} characters")
        elif not args.prompt.strip() or len(args.prompt) > analyst.MAX_PROMPT_CHARS:
            raise ValueError(f"--prompt must be 1-{analyst.MAX_PROMPT_CHARS} characters")
    except ValueError as exc:
        return _usage_error(str(exc))
    try:
        result = analyst.analyze(repo=args.repo, prompt=args.prompt, proposal_id=args.proposal_id,
                                 answer=args.answer, client=CrusoeClient.from_env())
    except Exception as exc:  # one-line error, never a traceback
        failure = _error(exc)
        if getattr(exc, "proposal_id", None):
            failure["proposal_id"] = exc.proposal_id
        print(json.dumps(failure), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print("\n".join(_summary_lines(result)), file=sys.stderr)
    return 0


def main_approve(args):
    try:
        analyst.check_id(args.proposal_id)
        analyst.check_id(args.session_id, "session id")
    except ValueError as exc:
        return _usage_error(str(exc))
    try:
        result = analyst.approve(args.proposal_id, session_id=args.session_id)
    except Exception as exc:
        print(json.dumps(_error(exc)), file=sys.stderr)
        return 1
    if result["status"] != "prepared":
        print(json.dumps(result), file=sys.stderr)
        print("Prepare failed; the error was recorded. Adjust with: python -m agent_team analyze "
              f'--proposal-id {args.proposal_id} --answer "..."', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2), file=sys.stderr if args.run else sys.stdout)
    if not args.run:
        print(f"Prepared session {args.session_id}. Start it with: python -m agent_team run "
              f"--session-id {args.session_id} --check", file=sys.stderr)
        return 0
    return main_run(argparse.Namespace(
        session_id=args.session_id, output=None, deadline=None, max_rounds=None, max_cost_usd=None,
        max_tokens=None, inject_failures=False, check=False))


if __name__ == "__main__":
    sys.exit(main())
