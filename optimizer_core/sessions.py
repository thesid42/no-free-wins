"""Private seed commitments prepared before any worker sees the task."""

from pathlib import Path
import re
import secrets
from .canonical import canonical_json, digest, strict_json


def _path(session_id, root):
    if type(session_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("session ID must use letters, digits, underscores or hyphens")
    return Path(root) / session_id / "session.json"


def prepare_session(session_id, *, workload_lines=100_000, root="private/sessions"):
    if type(workload_lines) is not int or not 1 <= workload_lines <= 100_000:
        raise ValueError("invalid workload size")
    path = _path(session_id, root)
    if path.exists():
        existing = load_session(session_id, root=root)
        if existing["workload_lines"] != workload_lines:
            raise ValueError("existing session workload cannot change")
        return {key: value for key, value in existing.items() if key != "seed"}
    seed = secrets.token_hex(16)
    data = {"schema_version": 1, "session_id": session_id, "seed": seed,
            "seed_sha256": digest(seed.encode()), "workload_lines": workload_lines,
            "generator_sha256": digest(Path(__file__).with_name("tasks.py").read_bytes())}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(canonical_json(data).decode())
    path.chmod(0o600)
    return {key: value for key, value in data.items() if key != "seed"}


def load_session(session_id, *, root="private/sessions"):
    path = _path(session_id, root)
    if not path.exists():
        raise ValueError("Prepare this session before starting agents")
    data = strict_json(path.read_bytes())
    generator = "repo_task.py" if "repo" in data else "tasks.py"
    if (data["session_id"] != session_id or
            digest(data["seed"].encode()) != data["seed_sha256"] or
            digest(Path(__file__).with_name(generator).read_bytes()) != data["generator_sha256"]):
        raise ValueError("frozen session seed/generator changed; use a new session")
    return data
