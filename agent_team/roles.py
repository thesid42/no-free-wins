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
"Exactly matches" means re.fullmatch semantics on the whole string: a line with a trailing
newline, any leading or trailing whitespace, extra tokens, a negative latency, or any non-ASCII
character is invalid and ignored. Do not use re.match with "$" (it accepts a trailing "\\n") and
do not use str.split()/strip() (they hide surrounding whitespace). ASCII digits only.
isdigit() alone accepts extra Unicode digits and is insufficient validation.
Public examples: 'INFO user=alice latency=0003' is valid; that same string followed by a
newline is invalid; 'INFO\\tuser=alice latency=3' and 'INFO user=a! latency=3' are invalid.
Return {"by_level": counts for all four levels (DEBUG, INFO, WARN, ERROR), including zeros,
"by_user": {user: {"count": n, "latency_sum": total}}}.
Use integer arithmetic only: no floats, booleans, nulls or custom/lazy objects.
Output is judged against a reference on hidden inputs and timed against a slow baseline that
parses lines and then rescans them once per user. Faster honest algorithms are the goal.
The target is at least 3x speedup while preserving all validation and output requirements.
No special-casing of inputs, no clock patching, no early exit, no lazy results."""

# Fences pair an opener (with optional tag) to the next bare ```, so a closing fence is never
# mistaken for an opener; only blocks tagged exactly "python" count as candidate code.
_FENCE = re.compile(r"```([\w+.-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_PYTHON_TAGS = {"python", "py", "python3"}
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_reasoning(text):
    """Drop <think> reasoning so only the final answer is parsed. Complete blocks go first; a
    stray </think> (opening tag lived in the prompt) drops everything before the last one, unless
    that reasoning contains a "{" (it may hold a draft answer: fail closed); a leading <think>
    that never closes means the answer never arrived, so nothing is left."""
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text:
        before, _, text = text.rpartition("</think>")
        if "{" in before:
            raise MalformedOutput("reasoning with a JSON-like object but no opening <think>")
    if text.lstrip().startswith("<think>"):
        return ""
    return text


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("unsupported number")


_DECODER = json.JSONDecoder(object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
_OPENER = re.compile(r"[{\[]")
MAX_JSON_REPLY_CHARS = 64 * 1024
MAX_FAILED_DECODES = 256


def _dicts_with_keys(value, keys):
    """Every dict carrying the keys, at any depth, as (dict, nested) with nested False only for the
    decoded value itself. Iterative: decoded depth is bounded but the walk must not recurse."""
    found, stack = [], [(value, False)]
    while stack:
        item, nested = stack.pop()
        if type(item) is dict:
            if keys <= set(item):
                found.append((item, nested))
            stack.extend((child, True) for child in item.values())
        elif type(item) is list:
            stack.extend((child, True) for child in item)
    return found


def _json_object(text, keys, normalize, marker):
    """Find the one JSON object carrying the required keys in the reply (prose, fences or quoted
    code around it are fine) and fail closed on anything unclear. Every matching dict at any depth
    counts; one nested inside an array or another object is never accepted; several top-level ones
    must agree on the required fields; and the number of quoted `marker` keys in the raw text must
    equal the number of objects found, so an undecodable draft (NaN, duplicate key, trailing
    comma, single quotes) cannot hide next to a decodable one. Extra keys are dropped by
    normalize(), which raises MalformedOutput on a bad shape."""
    if type(text) is not str:
        raise MalformedOutput("reply must be text")
    text = _strip_reasoning(text)
    if len(text) > MAX_JSON_REPLY_CHARS:
        raise MalformedOutput("reply too long to parse")
    found, index, failures = [], 0, 0
    while True:
        match = _OPENER.search(text, index)
        if match is None:
            break
        index = match.start()
        try:
            value, end = _DECODER.raw_decode(text, index)
        except (ValueError, RecursionError):
            failures += 1
            if failures > MAX_FAILED_DECODES:
                raise MalformedOutput("too many undecodable JSON openers") from None
            index += 1
            continue
        found.extend(_dicts_with_keys(value, keys))
        index = end
    quoted = len(re.findall(r"""["']""" + marker + r"""["']\s*:""", text))
    if quoted != len(found):
        raise MalformedOutput(f"ambiguous: {quoted} quoted {marker!r} keys but {len(found)} objects")
    if not found:
        raise MalformedOutput("no JSON object with the required keys")
    if any(nested for _, nested in found):
        raise MalformedOutput("ambiguous: matching object nested inside another value")
    normalized = [normalize(item) for item, _ in found]
    if any(item != normalized[0] for item in normalized[1:]):
        raise MalformedOutput("ambiguous: several differing JSON objects with the required keys")
    return normalized[0]


def _normalize_plan(value):
    plan = value["strategies"]
    if type(plan) is not list:
        raise MalformedOutput("strategies must be a list")
    entries = []
    for item in plan:
        if (type(item) is not dict or type(item.get("strategy_id")) is not str
                or type(item.get("description")) is not str):
            raise MalformedOutput("invalid strategy entry")
        entries.append({"strategy_id": item["strategy_id"], "description": item["description"]})
    return {"strategies": entries}


def parse_plan(text):
    return _json_object(text, {"strategies"}, _normalize_plan, "strategies")["strategies"]


def distinct_strategies(strategies):
    """At least two entries with unique, non-empty strategy ids."""
    if type(strategies) is not list or len(strategies) < 2:
        return False
    ids = [item.get("strategy_id") if type(item) is dict else None for item in strategies]
    return all(type(i) is str and i for i in ids) and len(set(ids)) == len(ids)


def parse_candidate(text):
    if type(text) is not str:
        raise MalformedOutput("reply must be text")
    blocks = [body for tag, body in _FENCE.findall(_strip_reasoning(text)) if tag.lower() in _PYTHON_TAGS]
    if len(blocks) != 1:
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


def _normalize_verdict(value):
    if type(value["approved"]) is not bool or type(value["candidate_sha256"]) is not str:
        raise MalformedOutput("invalid verdict field types")
    return {"approved": value["approved"], "candidate_sha256": value["candidate_sha256"]}


def parse_verdict(text, expected_sha256):
    verdict = _json_object(text, {"approved", "candidate_sha256"}, _normalize_verdict, "approved")
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
