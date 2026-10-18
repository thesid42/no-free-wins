"""Live-run wiring: Crusoe models, a priced budget, A's real harness, a session preflight.

Nothing here runs at import time. This is the ONE agent_team module that may reference
optimizer_core.harness (lazily, inside real_evaluate), optimizer_core.repo_task (lazily, inside
_task_context) or the daytona SDK (an installed-package check in the preflight). Importing
agent_team.live must not import any of them.
"""

import importlib.metadata
import math
from pathlib import Path
import re

from optimizer_core import config, sessions

from . import config as nfw_config, roles
from .budget import Budget
from .calls import ModelOrder
from .crusoe import CrusoeClient, prices_for, resolve_model
from .faults import FaultInjectingClient, drill_annotation, drill_plan
from .orchestrator import DEFAULT_MIN_HARNESS_SECONDS, MAX_DEADLINE_SECONDS, Orchestrator


def __getattr__(name):
    """LIVE_LINEUP (role -> (primary, fallback) NAMES, resolved against the live /models listing
    because ids differ in case/spelling) comes from the global nofreewins.config.json. It is read on
    access, so importing this module never touches the config file."""
    if name == "LIVE_LINEUP":
        return nfw_config.live_lineup()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Real evaluate_candidate provisions and probes a sandbox before its own deadline starts and cleans
# up after, so it can overrun remaining_wall_seconds: it is told less, and the floors are higher.
HARNESS_SETUP_MARGIN_SECONDS = 120
LIVE_HARNESS_FLOORS = {mode: seconds + HARNESS_SETUP_MARGIN_SECONDS
                       for mode, seconds in DEFAULT_MIN_HARNESS_SECONDS.items()}
MIN_CALL_TOKENS, MAX_CALL_TOKENS = nfw_config.MIN_CALL_TOKENS, nfw_config.MAX_CALL_TOKENS
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_OFFICIAL_COST_USD = nfw_config.MAX_OFFICIAL_COST_USD  # at most min($25, remaining)
RUN_LABEL = {"type": "run_label", "label": "development live run; not an official session",
             "harness": "optimizer_core real", "models": "crusoe live"}


LOGS_TASK_ID = "logs-parse-v1"
MAX_LABEL_CHARS = 200


def _scalar(value):
    """Annotation values must be scalars: anything else becomes bounded text."""
    if type(value) in (int, float, bool) and (type(value) is not float or math.isfinite(value)):
        return value
    return str(value)[:MAX_LABEL_CHARS]


def _task_context(session_id):
    """(task_id, task_summary, run_label annotation, task info) for a prepared session.

    A session without a repo block is the logs task, exactly as before. For a repo session the
    summary comes from RepoTask.public_summary(), which reads sealed files and never runs repo
    code; optimizer_core.repo_task is imported only here, only for repo sessions."""
    session = sessions.load_session(session_id)
    repo = session.get("repo")
    if repo is None:
        return (LOGS_TASK_ID, roles.TASK_SUMMARY, dict(RUN_LABEL),
                {"task_id": LOGS_TASK_ID, "kind": "logs", "include": None,
                 "n_files": None, "bundle_bytes": None})
    from optimizer_core.repo_task import RepoTask
    task = RepoTask.load(session)
    origin = repo.get("origin_info")
    origin = origin if type(origin) is dict else {}
    label = {"type": "run_label", "label": "development live run on user repo; not an official session",
             "task_id": _scalar(task.task_id), "repo_kind": _scalar(origin.get("kind", "")),
             "repo_commit": _scalar(origin.get("commit", "")),
             "bundle_sha256": _scalar(repo.get("bundle_sha256", ""))}
    return (task.task_id, task.public_summary(), label,
            {"task_id": task.task_id, "kind": "repo", "include": repo.get("include"),
             "n_files": repo.get("n_files"), "bundle_bytes": repo.get("bundle_bytes")})


DRILL_ROLE, DRILL_FAILURES = "worker_a", 2
DRILL_LABEL_SUFFIX = "; labeled fault drill"


class _RoleScopedFaults:
    """Route only the drilled role's calls through the fault client.

    drill_plan keys faults by model id, and the planner and both workers share a primary model, so
    an unscoped wrapper would spend the injected failures on whichever role calls first."""

    def __init__(self, inner, faulty, role):
        self._inner, self._faulty, self._role = inner, faulty, role

    @property
    def injected_total(self):
        return self._faulty.injected_total

    def _target(self, role):
        return self._faulty if role == self._role else self._inner

    def complete(self, model_id, role, messages, max_tokens):
        return self._target(role).complete(model_id, role, messages, max_tokens)

    def __getattr__(self, name):
        if name in ("_inner", "_faulty", "_role"):
            raise AttributeError(name)
        if name == "complete_with_timeout" and callable(getattr(self._faulty, name, None)):
            def complete_with_timeout(model_id, role, messages, max_tokens, timeout_seconds):
                return getattr(self._target(role), name)(model_id, role, messages, max_tokens,
                                                         timeout_seconds)
            return complete_with_timeout
        if name == "list_models":
            return getattr(self._inner, name)
        raise AttributeError(name)


def _drill_info(orders):
    plan = drill_plan(orders, DRILL_ROLE, DRILL_FAILURES)
    (model_id, failures), = plan.items()
    return {"role": DRILL_ROLE, "model_id": model_id, "failures": failures}


def resolve_lineup(available_ids, lineup=None):
    """role -> ModelOrder of live ids; a failed resolution is re-raised naming the role."""
    if lineup is None:
        lineup = nfw_config.live_lineup()
    orders = {}
    for role, (primary, fallback) in lineup.items():
        try:
            orders[role] = ModelOrder(resolve_model(primary, available_ids),
                                      resolve_model(fallback, available_ids))
        except ValueError as exc:
            raise ValueError(f"{role}: {exc}") from None
    return orders


def _model_ids(orders):
    return sorted({model for order in orders.values() for model in (order.primary, order.fallback)})


def check_cost(max_cost_usd):
    if (type(max_cost_usd) not in (int, float) or not math.isfinite(max_cost_usd)
            or not 0 < max_cost_usd <= MAX_OFFICIAL_COST_USD):
        raise ValueError(f"max_cost_usd must be greater than 0 and at most {MAX_OFFICIAL_COST_USD:g}")


def check_deadline(deadline_seconds):
    if (type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds)
            or not 0 < deadline_seconds <= MAX_DEADLINE_SECONDS):
        raise ValueError(f"deadline must be greater than 0 and at most {MAX_DEADLINE_SECONDS} seconds")


def check_max_tokens(max_tokens):
    if type(max_tokens) is not int or not MIN_CALL_TOKENS <= max_tokens <= MAX_CALL_TOKENS:
        raise ValueError(f"max tokens must be an integer from {MIN_CALL_TOKENS} to {MAX_CALL_TOKENS}")


def check_session_id(session_id):
    """Letters, digits, underscore, hyphen only: the id becomes part of a default output path."""
    if type(session_id) is not str or _SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("session id must be 1-128 letters, digits, underscores or hyphens")


def check_output_dir(output_dir):
    output = Path(output_dir)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("output directory exists and is not empty")


def live_budget(orders, *, max_cost_usd):
    """Priced Budget over every model the orders can call (an unknown price raises)."""
    check_cost(max_cost_usd)
    return Budget(max_attempts=60, max_tokens=180_000, max_cost_usd=max_cost_usd, task_attempts=12,
                  prices=prices_for(_model_ids(orders)))


def make_preflight(session_id):
    """Offline checks, run by the Orchestrator before any model call: nothing touches the network."""
    def preflight():
        sessions.load_session(session_id)  # raises unless the session was prepared
        config.DaytonaSettings.load().validate()  # raises without a Daytona key
        try:
            importlib.metadata.version("daytona")
        except importlib.metadata.PackageNotFoundError:
            raise RuntimeError("the daytona package is not installed") from None
    return preflight


def real_evaluate(request):
    """A's real harness (one disposable sandbox per evaluation). The only agent_team import of it.

    The harness is told HARNESS_SETUP_MARGIN_SECONDS less than the orchestrator has left. The
    change is made on a copy, so the caller's request is never altered."""
    from optimizer_core.harness import evaluate_candidate
    sent = dict(request)
    sent["remaining_wall_seconds"] = max(
        1.0, request["remaining_wall_seconds"] - HARNESS_SETUP_MARGIN_SECONDS)
    return evaluate_candidate(sent)


def _plan(available_ids, *, max_cost_usd, deadline_seconds, max_rounds, max_tokens):
    orders = resolve_lineup(available_ids)
    budget = live_budget(orders, max_cost_usd=max_cost_usd)
    snapshot = budget.snapshot()
    info = {"models": {role: {"primary": o.primary, "fallback": o.fallback}
                       for role, o in orders.items()},
            "prices": dict(budget.prices),
            "budget": {key: snapshot[key] for key in
                       ("max_attempts", "task_attempts", "max_tokens", "max_cost_usd", "priced")},
            "deadline_seconds": deadline_seconds, "max_rounds": max_rounds,
            "max_tokens_per_call": max_tokens, "harness_floors": dict(LIVE_HARNESS_FLOORS)}
    return orders, budget, info


def _run_defaults(deadline_seconds, max_rounds, max_cost_usd, max_tokens):
    """Fill unset (None) run settings from the global config."""
    if None in (deadline_seconds, max_rounds, max_cost_usd, max_tokens):
        run = nfw_config.load_config()["run"]
        deadline_seconds = run["deadline_seconds"] if deadline_seconds is None else deadline_seconds
        max_rounds = run["max_rounds"] if max_rounds is None else max_rounds
        max_cost_usd = run["max_cost_usd"] if max_cost_usd is None else max_cost_usd
        max_tokens = run["max_tokens_per_call"] if max_tokens is None else max_tokens
    return deadline_seconds, max_rounds, max_cost_usd, max_tokens


def check_live(session_id, *, deadline_seconds=None, max_rounds=None, max_cost_usd=None, max_tokens=None,
               client=None, available_ids=None, inject_failures=False):
    """Everything a run needs short of running: preflight, free /models listing, lineup, budget.
    Unset settings come from the global config."""
    deadline_seconds, max_rounds, max_cost_usd, max_tokens = _run_defaults(
        deadline_seconds, max_rounds, max_cost_usd, max_tokens)
    check_session_id(session_id)
    check_deadline(deadline_seconds)
    check_cost(max_cost_usd)
    check_max_tokens(max_tokens)
    make_preflight(session_id)()
    task = _task_context(session_id)[3]
    if available_ids is None:
        available_ids = (client or CrusoeClient.from_env()).list_models()
    orders, _, info = _plan(available_ids, max_cost_usd=max_cost_usd, deadline_seconds=deadline_seconds,
                            max_rounds=max_rounds, max_tokens=max_tokens)
    info["task"] = task
    if inject_failures:
        info["drill"] = _drill_info(orders)
    return info


def build_live_run(*, session_id, output_dir, deadline_seconds=None, max_rounds=None, max_cost_usd=None,
                   max_tokens=None, client=None, evaluate=None, available_ids=None,
                   inject_failures=False, extra_summary=None, parallel=None, screen_bar=None):
    """Return (Orchestrator, info). The output directory is created only after every check passes.
    Unset settings come from the global config. extra_summary (public text, e.g. lessons from an
    earlier attempt) is appended to the task summary the planner and workers see. parallel=None
    takes run.parallel from the config (workers, screens and verifiers overlap within a round)."""
    if parallel is None:
        parallel = nfw_config.load_config()["run"]["parallel"]
    if screen_bar is None:
        screen_bar = nfw_config.load_config()["run"]["screen_bar"]
    deadline_seconds, max_rounds, max_cost_usd, max_tokens = _run_defaults(
        deadline_seconds, max_rounds, max_cost_usd, max_tokens)
    check_session_id(session_id)
    check_deadline(deadline_seconds)
    check_cost(max_cost_usd)
    check_max_tokens(max_tokens)
    check_output_dir(output_dir)
    make_preflight(session_id)()  # an unprepared session leaves no run directory (the orchestrator repeats it)
    task_id, task_summary, run_label, task = _task_context(session_id)
    if extra_summary is not None:
        task_summary = f"{task_summary}\n\n{extra_summary}"
    if client is None:
        client = CrusoeClient.from_env()
    if available_ids is None:
        available_ids = client.list_models()
    orders, budget, info = _plan(available_ids, max_cost_usd=max_cost_usd,
                                 deadline_seconds=deadline_seconds, max_rounds=max_rounds,
                                 max_tokens=max_tokens)
    info["task"] = task
    annotations = [run_label]
    if inject_failures:  # labeled drill: worker_a's primary fails twice, the fallback must recover
        info["drill"] = _drill_info(orders)
        client = _RoleScopedFaults(client, FaultInjectingClient(client, faults=drill_plan(
            orders, DRILL_ROLE, DRILL_FAILURES)), DRILL_ROLE)
        run_label["label"] += DRILL_LABEL_SUFFIX
        annotations.insert(0, drill_annotation(info["drill"]["model_id"], DRILL_FAILURES))
    ledger = Path(output_dir) / "ledger.jsonl"
    orchestrator = Orchestrator(
        evaluate=evaluate or real_evaluate, client=client, budget=budget, ledger_path=ledger,
        session_id=session_id, orders=orders, deadline_seconds=deadline_seconds,
        max_rounds=max_rounds, max_tokens=max_tokens, annotations=annotations,
        task_id=task_id, task_summary=task_summary, parallel=parallel, screen_bar=screen_bar,
        preflight=make_preflight(session_id),
        min_harness_seconds=dict(LIVE_HARNESS_FLOORS))  # only ever raised above the defaults
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    return orchestrator, info
