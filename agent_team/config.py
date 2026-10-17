"""The one global settings file: nofreewins.config.json at the project root.

Strict on purpose: unknown keys, wrong types and out-of-range values are errors with a clear
message, never silently defaulted. Pure stdlib; importing this module reads nothing. The existing
caps (cost, deadline, tokens per call) still bound every value, and CLI flags override only within
them. NFW_CONFIG points at another file.
"""

import json
import math
import os
from pathlib import Path

from .orchestrator import MAX_DEADLINE_SECONDS
from .roles import _reject_constant, _unique_pairs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "nofreewins.config.json"
ENV_VAR = "NFW_CONFIG"
MAX_CONFIG_BYTES = 64 * 1024
MAX_OFFICIAL_COST_USD = 25.0  # PREP section 4: official run budget is at most min($25, remaining)
MIN_CALL_TOKENS, MAX_CALL_TOKENS = 256, 32768
MAX_QUESTION_ROUNDS = 20
MAX_PREPARE_ATTEMPTS, MAX_AUTO_RUNS, MAX_ANALYST_RETRIES = 5, 3, 4
MAX_TIMING_SIZE = 10_000_000
MAX_CORRECTNESS_SIZE = 1_000_000
MAX_CORRECTNESS_SIZES = 64
ROLES = ("analyst", "planner", "worker_a", "worker_b", "verifier")
RUN_ROLES = ("planner", "worker_a", "worker_b", "verifier")
INCLUDES = ("package", "full")


class ConfigError(ValueError):
    pass


def _number(value, where, *, upper):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= upper:
        raise ConfigError(f"{where} must be a number greater than 0 and at most {upper:g}")
    return value


def _integer(value, where, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{where} must be an integer from {low} to {high}")
    return value


def _section(config, name, required):
    section = config.get(name)
    if type(section) is not dict:
        raise ConfigError(f"{name} must be an object")
    missing, extra = set(required) - set(section), set(section) - set(required)
    if missing:
        raise ConfigError(f"{name} is missing: {', '.join(sorted(missing))}")
    if extra:
        raise ConfigError(f"{name} has unknown keys: {', '.join(sorted(extra))}")
    return section


def _models(section):
    models = {}
    for role in ROLES:
        pair = section.get(role)
        if (type(pair) is not list or len(pair) != 2
                or any(type(name) is not str or not name.strip() or len(name) > 200 for name in pair)):
            raise ConfigError(f"models.{role} must be [primary, fallback] model names")
        if pair[0].lower() == pair[1].lower():
            raise ConfigError(f"models.{role}: primary and fallback must be different")
        models[role] = (pair[0], pair[1])
    return models


def validate_config(config):
    """Return a normalized deep copy of a config dict, or raise ConfigError."""
    if type(config) is not dict:
        raise ConfigError("config must be a JSON object")
    extra = set(config) - {"models", "run", "analysis", "repo", "auto"}
    if extra:
        raise ConfigError(f"unknown top-level keys: {', '.join(sorted(extra))}")
    models = _section(config, "models", ROLES)
    run = _section(config, "run", ("deadline_seconds", "max_rounds", "max_cost_usd",
                                   "max_tokens_per_call"))
    analysis = _section(config, "analysis", ("max_cost_usd", "max_question_rounds",
                                             "max_tokens_per_call"))
    repo = _section(config, "repo", ("include", "timing_size", "correctness_sizes"))
    auto = _section(config, "auto", ("max_prepare_attempts", "max_runs", "max_total_cost_usd",
                                      "max_analyst_retries"))
    sizes = repo["correctness_sizes"]
    if type(sizes) is not list or not 1 <= len(sizes) <= MAX_CORRECTNESS_SIZES:
        raise ConfigError(f"repo.correctness_sizes must be a list of 1 to {MAX_CORRECTNESS_SIZES} integers")
    if repo["include"] not in INCLUDES:
        raise ConfigError("repo.include must be 'package' or 'full'")
    return {
        "models": _models(models),
        "run": {
            "deadline_seconds": _number(run["deadline_seconds"], "run.deadline_seconds",
                                        upper=MAX_DEADLINE_SECONDS),
            "max_rounds": _integer(run["max_rounds"], "run.max_rounds", 1, 100),
            "max_cost_usd": _number(run["max_cost_usd"], "run.max_cost_usd",
                                    upper=MAX_OFFICIAL_COST_USD),
            "max_tokens_per_call": _integer(run["max_tokens_per_call"], "run.max_tokens_per_call",
                                            MIN_CALL_TOKENS, MAX_CALL_TOKENS)},
        "analysis": {
            "max_cost_usd": _number(analysis["max_cost_usd"], "analysis.max_cost_usd",
                                    upper=MAX_OFFICIAL_COST_USD),
            "max_question_rounds": _integer(analysis["max_question_rounds"],
                                            "analysis.max_question_rounds", 1, MAX_QUESTION_ROUNDS),
            "max_tokens_per_call": _integer(analysis["max_tokens_per_call"],
                                            "analysis.max_tokens_per_call",
                                            MIN_CALL_TOKENS, MAX_CALL_TOKENS)},
        "repo": {
            "include": repo["include"],
            "timing_size": _integer(repo["timing_size"], "repo.timing_size", 1, MAX_TIMING_SIZE),
            "correctness_sizes": [_integer(size, "repo.correctness_sizes entries", 0,
                                           MAX_CORRECTNESS_SIZE) for size in sizes]},
        "auto": {
            "max_prepare_attempts": _integer(auto["max_prepare_attempts"], "auto.max_prepare_attempts",
                                             1, MAX_PREPARE_ATTEMPTS),
            "max_runs": _integer(auto["max_runs"], "auto.max_runs", 1, MAX_AUTO_RUNS),
            "max_total_cost_usd": _number(auto["max_total_cost_usd"], "auto.max_total_cost_usd",
                                          upper=MAX_OFFICIAL_COST_USD),
            "max_analyst_retries": _integer(auto["max_analyst_retries"], "auto.max_analyst_retries",
                                            0, MAX_ANALYST_RETRIES)}}


def config_path(path=None):
    if path is not None:
        return Path(path)
    override = os.environ.get(ENV_VAR)
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_config(path=None):
    """Read and validate the config; path > NFW_CONFIG > the project root file."""
    target = config_path(path)
    try:
        if target.stat().st_size > MAX_CONFIG_BYTES:
            raise ConfigError(f"config file {target} is too large")
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {target}: {exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"config file {target} is not UTF-8") from None
    try:
        value = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ConfigError(f"config file {target} is not valid JSON: {exc}"[:300]) from None
    try:
        return validate_config(value)
    except ConfigError as exc:
        raise ConfigError(f"{target}: {exc}") from None


def role_lineup(config, roles):
    """role -> (primary, fallback) names for the given roles."""
    return {role: tuple(config["models"][role]) for role in roles}


def live_lineup(config=None):
    """The planner, worker and verifier lineup of a live run (the analyst has its own)."""
    return role_lineup(config or load_config(), RUN_ROLES)

