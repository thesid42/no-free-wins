"""Strict candidate output parsing and deterministic host-side serialization."""

import hashlib
import json


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_number(value):
    raise ValueError(f"unsupported number: {value}")


def strict_json(data):
    """For trusted record schemas; rejects duplicate keys and NaN/Infinity."""
    return json.loads(data, object_pairs_hook=_unique_object,
                      parse_constant=_reject_number)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _validate_output(value):
    if type(value) in (str, int):
        return
    if type(value) is list:
        for item in value:
            _validate_output(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_output(item)
        return
    raise ValueError("output must contain only exact integers, strings, lists, objects")


def parse_output(data, *, max_bytes=1_048_576):
    if not isinstance(data, bytes):
        raise TypeError("candidate output must be bytes")
    if len(data) > max_bytes:
        raise ValueError("candidate output exceeds byte limit")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_float=_reject_number, parse_constant=_reject_number)
        _validate_output(value)
    except RecursionError as exc:
        raise ValueError("candidate output exceeds nesting limit") from exc
    return canonical_json(value)
