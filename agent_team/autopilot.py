"""Autopilot: one submission (repo + prompt) runs unattended until verified, not feasible or out of budget.

The submission is the single up-front approval, under a total inference-spend cap. It chains the
existing pieces without adding new powers: the autonomous analyst, the sandboxed prepare-repo (with
a repair loop that feeds each prepare error back to the analyst) and the live run (re-planned from
a public lessons summary when a run does not verify). Nothing here runs repo code; analyst, approve
and run are injected functions. Progress goes out as one NFW_PROGRESS line per event on stderr.
"""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
import types

from . import analyst, roles
from . import config as nfw_config

PROJECT_ROOT = nfw_config.PROJECT_ROOT
SESSION_PREFIX = re.compile(r"[A-Za-z0-9_-]{1,100}")
PROGRESS_PREFIX = "NFW_PROGRESS "
STEPS = ("analyze", "repair", "prepare", "run", "lessons", "done")
MAX_MESSAGE_CHARS = 500
MAX_LESSONS_CHARS = 1500
AUTO_FILE = "auto.json"
AUTO_VERSION = 1
LESSONS_HEADER = "Lessons from the previous attempt: "
SCREEN_BAR = 3.0
ANALYST_RETRY_DELAYS = (20, 60, 60, 60)  # seconds before analyst retry 1, 2, ...


def check_session_prefix(prefix):
    if type(prefix) is not str or SESSION_PREFIX.fullmatch(prefix) is None:
        raise ValueError("session prefix must be 1-100 letters, digits, underscores or hyphens")


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scrub(text):
    """Project root -> <project>, home -> ~ (longest prefix first)."""
    text = str(text)
    for prefix, label in ((PROJECT_ROOT, "<project>"), (PROJECT_ROOT.resolve(), "<project>"),
                          (Path.home(), "~"), (Path.home().resolve(), "~")):
        text = text.replace(str(prefix), label)
    return text


def print_progress(event):
    """One line on stderr: NFW_PROGRESS <compact JSON>."""
    print(PROGRESS_PREFIX + json.dumps(event, separators=(",", ":"), sort_keys=True),
          file=sys.stderr, flush=True)


def _error_text(exc):
    return scrub(f"{type(exc).__name__}: {exc}")[:300]


# ------------------------------------------------------------------ lessons


def _num(value):
    return round(value, 3) if type(value) in (int, float) and math.isfinite(value) else None


def build_lessons(run):
    """A short PUBLIC summary of one run for the next planner: candidate statuses, screen speedups,
    feedback codes and the terminal reason. Built only from the orchestrator's own ledger events and
    the allowlisted public feedback; it never contains candidate source."""
    events = [e for e in run.get("events", []) if type(e) is dict]
    parts = []
    counts = {"worker_failed": 0, "duplicate_candidate": 0}
    codes = []
    for event in events:
        kind = event.get("type")
        who = str(event.get("candidate_id", ""))[:64]
        rnd = event.get("round")
        if kind in counts:
            counts[kind] += 1
        elif kind == "candidate_created":
            parts.append(f"round {rnd}: candidate {who} ({str(event.get('strategy_id', ''))[:64]}) "
                         "was written")
        elif kind == "screen_result":
            speedup = _num(event.get("screen_median_speedup"))
            parts.append(f"round {rnd}: {who} screen status {str(event.get('status'))[:40]}, "
                         f"correct={event.get('correctness_passed')}, screen speedup {speedup}")
            if event.get("correctness_passed") is False:
                codes.append("correctness_failed")
            elif speedup is not None and speedup < SCREEN_BAR:
                codes.append("screen_below_bar")
        elif kind == "verifier_verdict":
            parts.append(f"round {rnd}: {who} verifier approved={event.get('approved')}")
            if event.get("approved") is not True:
                codes.append("verifier_rejected")
        elif kind == "acceptance_result":
            acceptance = event.get("acceptance")
            acceptance = acceptance if type(acceptance) is dict else {}
            parts.append(f"round {rnd}: {who} acceptance status {str(event.get('status'))[:40]}, "
                         f"median speedup {_num(acceptance.get('median_speedup'))}, "
                         f"ci lower {_num(acceptance.get('ci_lower'))}")
            codes.append(f"acceptance_{str(event.get('status'))[:40]}")
    for name, count in counts.items():
        if count:
            parts.append(f"{name} x{count}")
    best = run.get("best_response")
    if type(best) is dict:
        for entry in roles.public_feedback([best]):
            codes += [item["code"][:60] for item in entry["feedback"]]
    terminal = next((e for e in reversed(events) if e.get("type") == "terminal"), {})
    state = str(run.get("state") or terminal.get("state"))[:60]
    reason = str(terminal.get("reason", ""))[:200]
    codes = list(dict.fromkeys(codes))
    text = f"The run ended {state} ({reason}). Try a materially different optimization than the " \
           "ones that failed, and keep the output identical to the original on every input."
    if codes:
        text += " Feedback codes: " + ", ".join(codes) + "."
    if parts:
        text += " Details: " + "; ".join(parts) + "."
    return LESSONS_HEADER + text[:MAX_LESSONS_CHARS]


# ------------------------------------------------------------------ default run


def default_run(*, session_id, output_dir, extra_summary=None, cfg=None):
    """One live run in-process through the CLI's run_live (config defaults), as a plain dict."""
    from . import __main__ as cli  # lazy: the CLI imports this module
    run = (cfg or nfw_config.load_config())["run"]
    args = types.SimpleNamespace(
        session_id=session_id, output=Path(output_dir), deadline=run["deadline_seconds"],
        max_rounds=run["max_rounds"], max_cost_usd=run["max_cost_usd"],
        max_tokens=run["max_tokens_per_call"], inject_failures=False, extra_summary=extra_summary)
    result, summary = cli.run_live(args)
    return {**summary, "events": result.events, "best_response": result.best_response}


# ------------------------------------------------------------------ pipeline


class _Stop(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def run_autopilot(*, repo, prompt, session_prefix, client=None, cfg=None, emit=print_progress,
                  analyze_fn=analyst.analyze, approve_fn=analyst.approve, run_fn=default_run,
                  now=_utc_now, new_id=None, proposals_root=analyst.DEFAULT_PROPOSALS_ROOT,
                  runs_root=None, max_prepare_attempts=None, max_runs=None, sleep=time.sleep):
    """Analyze, prepare (with repair) and run until verified, not feasible or out of budget.

    run_fn(session_id=, output_dir=, extra_summary=) returns a dict with at least run_id, state,
    ledger_final_sha256, budget (with cost_usd), events (the run's ledger events) and optionally
    best_response. Returns {status: verified_done | not_feasible | gave_up, proposal_id,
    assumptions, attempts, runs, final, report, total_cost_usd}.

    An analyst step that raises AnalysisFailed (model outage, malformed replies) is retried up to
    auto.max_analyst_retries times after 20 s, then 60 s (sleep is injectable). A retry resumes the
    proposal from its saved history (analyze with no new answer), so an answer is never duplicated."""
    check_session_prefix(session_prefix)
    cfg = cfg or nfw_config.load_config()
    auto = cfg["auto"]
    max_prepare = min(max_prepare_attempts or auto["max_prepare_attempts"],
                      nfw_config.MAX_PREPARE_ATTEMPTS)
    max_runs = min(max_runs or auto["max_runs"], nfw_config.MAX_AUTO_RUNS)
    max_total = min(auto["max_total_cost_usd"], nfw_config.MAX_OFFICIAL_COST_USD)
    if client is None:
        from .crusoe import CrusoeClient
        client = CrusoeClient.from_env()
    runs_base = Path(runs_root) if runs_root is not None else PROJECT_ROOT / "artifacts" / "runs"
    ids = {"proposal_id": None, "session_id": None, "run_dir": None, "run_id": None}
    record = {"schema_version": AUTO_VERSION, "repo": str(repo), "prompt": prompt,
              "session_prefix": session_prefix, "started_at": now(), "status": "running",
              "limits": {"max_prepare_attempts": max_prepare, "max_runs": max_runs,
                         "max_total_cost_usd": max_total},
              "assumptions": [], "attempts": [], "runs": [], "events": [], "total_cost_usd": 0.0}
    spent = [0.0]

    def save():
        record["total_cost_usd"] = round(spent[0], 6)
        if ids["proposal_id"] is None:
            return
        directory = analyst._dir(proposals_root, ids["proposal_id"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        analyst._write_private(directory / AUTO_FILE, json.dumps(record, indent=2, sort_keys=True))

    def event(step, status, message, attempt=None, **override):
        ids.update({k: v for k, v in override.items() if k in ids})
        line = {"ts": now(), "step": step, "attempt": attempt, "status": status,
                "message": scrub(message)[:MAX_MESSAGE_CHARS], **ids}
        line["run_dir"] = scrub(line["run_dir"]) if line["run_dir"] else None
        record["events"].append(line)
        save()
        emit(line)

    def guard(step_cap, what):
        if spent[0] + step_cap > max_total:
            raise _Stop("gave_up", f"spend guard: {what} could cost up to ${step_cap:g} and "
                                   f"${spent[0]:.2f} of the ${max_total:g} total is already spent")

    def analysis_step(step, attempt, **kwargs):
        """One autonomous analyst call, retried with backoff on AnalysisFailed; returns its result
        or raises _Stop."""
        max_retries = min(auto["max_analyst_retries"], nfw_config.MAX_ANALYST_RETRIES)
        retry = 0
        while True:
            guard(cfg["analysis"]["max_cost_usd"], "an analysis step")
            if retry == 0:
                event(step, "started", "analyst working (no questions will be asked)", attempt)
            extra = {"new_id": new_id} if new_id is not None and "proposal_id" not in kwargs else {}
            call = kwargs
            if retry and ids["proposal_id"] is not None:
                # resume from the saved history: the answer (if any) is already in it
                call = {"proposal_id": ids["proposal_id"], "answer": None}
                extra = {}
            try:
                result = analyze_fn(client=client, cfg=cfg, autonomous=True,
                                    proposals_root=proposals_root, **extra, **call)
                break
            except analyst.AnalysisFailed as exc:
                spent[0] += getattr(exc, "cost_usd", 0.0) or 0.0
                ids["proposal_id"] = getattr(exc, "proposal_id", None) or ids["proposal_id"]
                detail = scrub(getattr(exc, "summary", "") or str(exc))[:300]
                if retry >= max_retries:
                    event(step, "failed", f"analysis failed: {detail}", attempt)
                    raise _Stop("gave_up", f"analysis failed: {detail}") from None
                delay = ANALYST_RETRY_DELAYS[min(retry, len(ANALYST_RETRY_DELAYS) - 1)]
                retry += 1
                record.setdefault("analyst_retries", []).append(
                    {"step": step, "retry": retry, "delay_seconds": delay, "error": detail})
                event(step, "failed", f"analysis failed: {detail}; retry {retry} of {max_retries} "
                                      f"in {delay}s", attempt)
                sleep(delay)
                event(step, "started", f"analyst retry {retry} of {max_retries}", attempt)
            except Exception as exc:
                ids["proposal_id"] = getattr(exc, "proposal_id", None) or ids["proposal_id"]
                event(step, "failed", f"analysis failed: {_error_text(exc)}", attempt)
                raise _Stop("gave_up", f"analysis failed: {_error_text(exc)}") from None
        spent[0] += result.get("cost_usd", 0.0) or 0.0
        ids["proposal_id"] = result.get("proposal_id") or ids["proposal_id"]
        record["assumptions"] = list(result.get("assumptions") or record["assumptions"])
        if result["status"] == "not_feasible":
            record["not_feasible"] = {"reason": result.get("reason"),
                                      "suggestions": result.get("suggestions", [])}
            event(step, "failed", f"not feasible: {result.get('reason')}", attempt)
            raise _Stop("not_feasible", str(result.get("reason")))
        if result["status"] != "proposal":
            event(step, "failed", f"analyst returned {result['status']}, not a proposal", attempt)
            raise _Stop("gave_up", f"analyst returned {result['status']} instead of a proposal")
        target = (result.get("proposal") or {}).get("target")
        event(step, "ok", f"proposal for {target}", attempt)
        return result

    def prepare_step():
        errors = []
        for attempt in range(1, max_prepare + 1):
            session_id = f"{session_prefix}-p{attempt}"
            event("prepare", "started", f"preparing session {session_id} in the sandbox", attempt,
                  session_id=session_id)
            try:
                result = approve_fn(ids["proposal_id"], session_id=session_id,
                                    proposals_root=proposals_root)
            except Exception as exc:
                record["attempts"].append({"attempt": attempt, "session_id": session_id,
                                           "status": "error", "error": _error_text(exc)})
                event("prepare", "failed", f"prepare errored: {_error_text(exc)}", attempt)
                raise _Stop("gave_up", f"prepare errored: {_error_text(exc)}") from None
            if result["status"] == "prepared":
                record["attempts"].append({"attempt": attempt, "session_id": session_id,
                                           "status": "prepared"})
                event("prepare", "ok", f"session {session_id} prepared", attempt)
                return session_id
            error = analyst._sanitize(str(result.get("error", "")))
            errors.append(error)
            record["attempts"].append({"attempt": attempt, "session_id": session_id,
                                       "status": "prepare_failed", "error": error})
            event("prepare", "failed", f"prepare failed: {error[-300:]}", attempt)
            if attempt == max_prepare:
                break
            answer = (f"Prepare failed: {error}. Fix the proposal so prepare succeeds (respect the "
                      "size/runtime/determinism limits).")[:analyst.MAX_ANSWER_CHARS]
            analysis_step("repair", attempt, proposal_id=ids["proposal_id"], answer=answer)
        raise _Stop("gave_up", f"prepare failed {max_prepare} times: " + " | ".join(
            e[-200:] for e in errors))

    def run_step(session_id):
        lessons = None
        for k in range(1, max_runs + 1):
            guard(cfg["run"]["max_cost_usd"], "a run")
            stamp = now().replace("-", "").replace(":", "")
            name = f"{session_id}-r{k}-{stamp}"
            run_dir = runs_base / name
            shown = f"artifacts/runs/{name}"
            event("run", "started", f"run {k} of {max_runs} started"
                  + (" with lessons from the previous run" if lessons else ""), k,
                  run_dir=shown, run_id=None)
            entry = {"session_id": session_id, "run_dir": shown, "run_id": None, "state": None,
                     "attempt": k, "lessons_in": lessons}
            record["runs"].append(entry)
            try:
                run = run_fn(session_id=session_id, output_dir=run_dir, extra_summary=lessons)
            except Exception as exc:
                entry["state"] = "error"
                entry["error"] = _error_text(exc)
                event("run", "failed", f"run errored: {_error_text(exc)}", k)
                raise _Stop("gave_up", f"run errored: {_error_text(exc)}") from None
            budget = run.get("budget") if type(run.get("budget")) is dict else {}
            spent[0] += budget.get("cost_usd", 0.0) or 0.0
            acceptance = (run.get("best_response") or {}).get("acceptance")
            acceptance = acceptance if type(acceptance) is dict else {}
            entry.update(run_id=run.get("run_id"), state=run.get("state"),
                         ledger_final_sha256=run.get("ledger_final_sha256"),
                         median_speedup=acceptance.get("median_speedup"),
                         ci=acceptance.get("ci_lower"), cost_usd=budget.get("cost_usd"))
            ids["run_id"] = run.get("run_id")
            if run.get("state") == "verified_done":
                event("run", "ok", f"run {k} verified_done", k)
                return
            event("run", "failed", f"run {k} ended {run.get('state')}", k)
            if k == max_runs:
                break
            lessons = build_lessons(run)
            entry["lessons_out"] = lessons
            event("lessons", "ok", "lessons for the next run: " + lessons[len(LESSONS_HEADER):], k)
        raise _Stop("gave_up", f"no verified result after {max_runs} runs")

    status, message = "gave_up", ""
    try:
        analysis_step("analyze", 1, repo=repo, prompt=prompt)
        session_id = prepare_step()
        run_step(session_id)
        status, message = "verified_done", "verified result reached"
    except _Stop as stop:
        status, message = stop.status, stop.message
    except Exception as exc:  # never a traceback out of the autopilot
        status, message = "gave_up", f"unexpected {_error_text(exc)}"
    record["status"] = status
    record["message"] = scrub(message)[:MAX_MESSAGE_CHARS]
    last = next((r for r in reversed(record["runs"]) if r.get("state")), None)
    final = ({"run_dir": last["run_dir"], "state": last["state"],
              "ledger_final_sha256": last.get("ledger_final_sha256")} if last else None)
    runs = [{key: r[key] for key in ("session_id", "run_dir", "run_id", "state") if key in r}
            | {key: r[key] for key in ("median_speedup", "ci") if r.get(key) is not None}
            for r in record["runs"]]
    report = _report(status, record, runs, message, spent[0])
    record.update(final=final, report=report)
    event("done", "ok" if status == "verified_done" else "failed", f"{status}: {report}")
    return {"status": status, "proposal_id": ids["proposal_id"],
            "assumptions": record["assumptions"], "attempts": record["attempts"], "runs": runs,
            "final": final, "report": report, "total_cost_usd": round(spent[0], 6),
            **({"not_feasible": record["not_feasible"]} if "not_feasible" in record else {})}


def _report(status, record, runs, message, spent):
    lead = {"verified_done": "Verified: the optimized function passed measured acceptance",
            "not_feasible": "Not feasible",
            "gave_up": "Gave up"}[status]
    detail = scrub(message)[:300]
    speed = next((r["median_speedup"] for r in reversed(runs) if "median_speedup" in r), None)
    text = f"{lead}"
    if status == "verified_done" and speed is not None:
        text += f" (median speedup {speed:.2f}x)"
    elif detail and status != "verified_done":
        text += f": {detail}"
    return (f"{text}. {len(record['attempts'])} prepare attempt(s), {len(runs)} run(s), "
            f"estimated inference cost ${spent:.2f}.")
