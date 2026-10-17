"""Prompt builders and strict parsers for planner, worker and verifier roles. Pure functions."""

import ast
import json
import math
import re

from optimizer_core.canonical import strict_json

from .models import MalformedOutput

TASK_SUMMARY = """\
Task logs-parse-v1. Write a Python function solve(lines) in standard-library Python.
lines is a list of strings. A line is valid only if it exactly matches the regex:
  (DEBUG|INFO|WARN|ERROR) user=([A-Za-z0-9_-]{1,32}) latency=([0-9]{1,7})
Other lines are ignored. Leading zeros in latency digits are allowed.
Return {"by_level": counts for all four levels (DEBUG, INFO, WARN, ERROR), including zeros,
"by_user": {user: {"count": n, "latency_sum": total}}}.
Use integer arithmetic only: no floats, booleans, nulls or custom/lazy objects.
Output is judged against a reference on hidden inputs and timed against a slow baseline that
parses lines and then rescans them once per user. Faster honest algorithms are the goal.
No special-casing of inputs, no clock patching, no early exit, no lazy results."""

_FENCE = re.compile(r"```python[ \t]*\r?\n(.*?)```", re.DOTALL)


def _json_object(text, keys):
    try:
        value = strict_json(text.strip())
    except (ValueError, RecursionError, AttributeError) as exc:
        raise MalformedOutput("invalid JSON") from exc
    if type(value) is not dict or set(value) != keys:
        raise MalformedOutput("unexpected JSON shape")
    return value


def parse_plan(text):
    plan = _json_object(text, {"strategies"})["strategies"]
    if type(plan) is not list:
        raise MalformedOutput("strategies must be a list")
    for item in plan:
        if (type(item) is not dict or set(item) != {"strategy_id", "description"} or
                type(item["strategy_id"]) is not str or type(item["description"]) is not str):
            raise MalformedOutput("invalid strategy entry")
    return plan


def distinct_strategies(strategies):
    """At least two entries with unique, non-empty strategy ids."""
    if type(strategies) is not list or len(strategies) < 2:
        return False
    ids = [item.get("strategy_id") if type(item) is dict else None for item in strategies]
    return all(type(i) is str and i for i in ids) and len(set(ids)) == len(ids)


def parse_candidate(text):
    if type(text) is not str:
        raise MalformedOutput("reply must be text")
    blocks = _FENCE.findall(text)
    if len(blocks) != 1 or text.count("```") != 2:
        raise MalformedOutput("expected exactly one python code block")
    source = blocks[0]
    try:
        encoded = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MalformedOutput("candidate is not valid UTF-8") from exc
    if len(encoded) > 262_144:
        raise MalformedOutput("candidate exceeds the source size limit")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise MalformedOutput("candidate does not parse") from exc
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "solve":
            a = node.args
            if (len(a.posonlyargs) + len(a.args) == 1 and a.vararg is None
                    and not a.kwonlyargs and a.kwarg is None):
                return source
    raise MalformedOutput("missing top-level solve(lines)")


def parse_verdict(text, expected_sha256):
    verdict = _json_object(text, {"approved", "candidate_sha256", "rationale"})
    if (type(verdict["approved"]) is not bool or type(verdict["candidate_sha256"]) is not str
            or type(verdict["rationale"]) is not str):
        raise MalformedOutput("invalid verdict field types")
    if verdict["candidate_sha256"] != expected_sha256:
        return {**verdict, "approved": False, "mismatch": True}
    return verdict


def planner_messages(task_summary):
    return [{"role": "system", "content": "You plan optimization strategies. Reply with JSON only."},
            {"role": "user", "content": (
                f"{task_summary}\n\nPropose at least two distinct optimization strategies as JSON: "
                '{"strategies": [{"strategy_id": "<short-id>", "description": "<one paragraph>"}]}')}]


def worker_messages(task_summary, strategy, feedback):
    return [{"role": "system", "content": "You write Python solutions. Reply with one python code block."},
            {"role": "user", "content": (
                f"{task_summary}\n\nStrategy {strategy['strategy_id']}: {strategy['description']}\n\n"
                "Public evaluation feedback so far (JSON):\n"
                f"{json.dumps(sanitize_feedback(feedback), sort_keys=True)}\n\n"
                "Reply with exactly one ```python block defining solve(lines).")}]


def verifier_messages(task_summary, candidate_source, candidate_sha256):
    return [{"role": "system", "content": "You review candidate code for honesty and correctness. Reply with JSON only."},
            {"role": "user", "content": (
                f"{task_summary}\n\nCandidate sha256: {candidate_sha256}\n"
                f"Candidate source:\n```python\n{candidate_source}\n```\n\n"
                "Approve only an honest, general implementation. Reply with JSON: "
                '{"approved": true|false, "candidate_sha256": "<sha256 above>", "rationale": "<text>"}')}]


def _items(feedback):
    """Keep only entries with a str code; forward code/summary as str, evidence ids as list of str."""
    result = []
    for item in feedback if type(feedback) is list else []:
        if type(item) is not dict or type(item.get("code")) is not str:
            continue
        ids = item.get("evidence_ids")
        result.append({"code": item["code"],
                       "summary": item["summary"] if type(item.get("summary")) is str else "",
                       "evidence_ids": [e for e in ids if type(e) is str] if type(ids) is list else []})
    return result


def _status(value):
    return value if type(value) is str else None


def _speedup(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def sanitize_feedback(feedback):
    """Allowlist and type-check feedback for workers: public_feedback() shape or {"code": str} dicts."""
    result = []
    for entry in feedback if type(feedback) is list else []:
        if type(entry) is not dict:
            continue
        out = {}
        if {"status", "screen_median_speedup", "feedback"} & set(entry):
            out = {"status": _status(entry.get("status")),
                   "screen_median_speedup": _speedup(entry.get("screen_median_speedup")),
                   "feedback": _items(entry.get("feedback"))}
        for item in _items([entry]):
            out.update(item)
        if out:
            result.append(out)
    return result


def public_feedback(responses):
    """Allowlisted view of harness responses for workers; nothing else is ever forwarded."""
    return [{"status": _status(resp.get("status")),
             "screen_median_speedup": _speedup(resp.get("screen_median_speedup")),
             "feedback": _items(resp.get("feedback_public"))}
            for resp in responses if type(resp) is dict]
