"""Live-run wiring: Crusoe models, a priced budget, A's real harness, a session preflight.

Nothing here runs at import time. This is the ONE agent_team module that may reference
optimizer_core.harness (lazily, inside real_evaluate) or the daytona SDK (an installed-package
check in the preflight). Importing agent_team.live must not import either.
"""

import importlib.metadata
import math
from pathlib import Path
import re

from optimizer_core import config, sessions

from .budget import Budget
from .calls import ModelOrder
from .crusoe import CrusoeClient, prices_for, resolve_model
from .orchestrator import DEFAULT_MIN_HARNESS_SECONDS, MAX_DEADLINE_SECONDS, Orchestrator

_PLANNER_WORKER = ("nemotron-3-super", "nemotron-3-nano-30b")
# Names, not ids: they are resolved against the live /models listing (ids differ in case/spelling).
LIVE_LINEUP = {"planner": _PLANNER_WORKER, "worker_a": _PLANNER_WORKER, "worker_b": _PLANNER_WORKER,
               "verifier": ("glm-5.3-flash", "deepseek-v4-flash")}
# Real evaluate_candidate provisions and probes a sandbox before its own deadline starts and cleans
# up after, so it can overrun remaining_wall_seconds: it is told less, and the floors are higher.
HARNESS_SETUP_MARGIN_SECONDS = 120
LIVE_HARNESS_FLOORS = {mode: seconds + HARNESS_SETUP_MARGIN_SECONDS
                       for mode, seconds in DEFAULT_MIN_HARNESS_SECONDS.items()}
MIN_CALL_TOKENS, MAX_CALL_TOKENS = 256, 32768
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_OFFICIAL_COST_USD = 25.0  # PREP section 4: official run budget is at most min($25, remaining)
RUN_LABEL = {"type": "run_label", "label": "development live run; not an official session",
             "harness": "optimizer_core real", "models": "crusoe live"}


def resolve_lineup(available_ids, lineup=LIVE_LINEUP):
    """role -> ModelOrder of live ids; a failed resolution is re-raised naming the role."""
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


def check_live(session_id, *, deadline_seconds=1800, max_rounds=3, max_cost_usd=5.0, max_tokens=8192,
               client=None, available_ids=None):
    """Everything a run needs short of running: preflight, free /models listing, lineup, budget."""
    check_session_id(session_id)
    check_deadline(deadline_seconds)
    check_cost(max_cost_usd)
    check_max_tokens(max_tokens)
    make_preflight(session_id)()
    if available_ids is None:
        available_ids = (client or CrusoeClient.from_env()).list_models()
    return _plan(available_ids, max_cost_usd=max_cost_usd, deadline_seconds=deadline_seconds,
                 max_rounds=max_rounds, max_tokens=max_tokens)[2]


def build_live_run(*, session_id, output_dir, deadline_seconds=1800, max_rounds=3, max_cost_usd=5.0,
                   max_tokens=8192, client=None, evaluate=None, available_ids=None):
    """Return (Orchestrator, info). The output directory is created only after every check passes."""
    check_session_id(session_id)
    check_deadline(deadline_seconds)
    check_cost(max_cost_usd)
    check_max_tokens(max_tokens)
    check_output_dir(output_dir)
    make_preflight(session_id)()  # an unprepared session leaves no run directory (the orchestrator repeats it)
    if client is None:
        client = CrusoeClient.from_env()
    if available_ids is None:
        available_ids = client.list_models()
    orders, budget, info = _plan(available_ids, max_cost_usd=max_cost_usd,
                                 deadline_seconds=deadline_seconds, max_rounds=max_rounds,
                                 max_tokens=max_tokens)
    ledger = Path(output_dir) / "ledger.jsonl"
    orchestrator = Orchestrator(
        evaluate=evaluate or real_evaluate, client=client, budget=budget, ledger_path=ledger,
        session_id=session_id, orders=orders, deadline_seconds=deadline_seconds,
        max_rounds=max_rounds, max_tokens=max_tokens, annotations=[dict(RUN_LABEL)],
        preflight=make_preflight(session_id),
        min_harness_seconds=dict(LIVE_HARNESS_FLOORS))  # only ever raised above the defaults
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    return orchestrator, info
