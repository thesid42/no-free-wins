"""Repo tasks: optimize one function of a small pure-stdlib repo against its own generator.

Nothing here imports or executes repo code on the controller. Specs and sources are only
parsed with ast; the repo ships to the sandbox as a compressed bundle and every generator,
baseline and candidate run goes through backend.run_trial.
"""

import ast
import base64
import hashlib
import io
import keyword
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
from statistics import median
import uuid
import zlib

from .canonical import canonical_json, digest, strict_json
from .harness import CandidateFailure, checked_trial, correctness_batches, parse_results

SANDBOX_SOURCE_MAX = 262_144          # frozen sandbox limit on the executed source
REPO_BUNDLE_MAX = 196_608             # prelude + bundle; the rest is room for the candidate
MAX_SOURCE_BYTES = 8 << 20            # uncompressed repo sources
MAX_CLONE_BYTES = 50 << 20
MAX_VALUE_BYTES = 1 << 20             # one generated input / one trial's output
MAX_SIZE = 10_000_000
MAX_SPEC_BYTES = 64 << 10
MAX_FILE_BYTES = 1 << 20              # per source file
MAX_BASELINE_SECONDS = 1.0            # median warmup run; the harness cap is 2 s per run
CORRECTNESS_CASES = 2000
DEFAULT_CORRECTNESS_SIZES = [0, 1, 2, 3, 5, 8, 13, 50, 200, 1000]
EXCLUDED_DIRS = {"tests", "test", ".git", ".venv", "venv", "__pycache__", "build", "dist"}
SEALED_FILES = ("spec.json", "bundle.b64", "cases.json", "expected.json",
                "workloads.json", "public.json")
SPEC_KEYS = {"target", "generator", "include", "timing_size", "correctness_sizes", "description"}
DEFAULTS = {"include": "full", "timing_size": 40000,
            "correctness_sizes": [0, 1, 2, 3, 5, 8, 13, 50, 200, 1000]}
GENERATED_DIR = "_nfw_generated"
GENERATED_REF = f"{GENERATED_DIR}/gen.py:generate_input"
PROPOSAL_KEYS = {"target", "generator_source", "timing_size", "correctness_sizes", "include",
                 "instruction", "hints", "description"}
GENERATOR_SOURCE_MAX = 32 << 10
FORBIDDEN_IMPORTS = {"os", "subprocess", "socket", "ctypes", "importlib", "builtins", "posix",
                     "nt", "io", "sys", "pathlib", "shutil", "pty", "multiprocessing", "runpy",
                     "pickle", "marshal", "tempfile", "glob", "signal", "threading"}
FORBIDDEN_NAMES = {"open", "eval", "exec", "__import__", "compile", "getattr", "setattr",
                   "delattr", "globals", "locals", "vars"}
FORBIDDEN_ATTRS = {"__builtins__", "__import__", "__globals__", "__subclasses__", "__code__",
                   "modules"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHASES = (("warmup", 5), ("screen", 5), ("acceptance", 30))
_GITHUB = re.compile(r"https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100}?)"
                     r"(?:\.git)?(?:@([A-Za-z0-9._/-]{1,200}))?")
_MARKER = "@@NFW_BUNDLE@@"


def _task_id(bundle_sha256, spec_sha256):
    return "repo-" + digest(canonical_json({"bundle": bundle_sha256, "spec": spec_sha256}))[:12]

PRELUDE = '''\
import base64 as _nfw_base64, importlib.abc as _nfw_abc, importlib.util as _nfw_util
import json as _nfw_json, sys as _nfw_sys, zlib as _nfw_zlib
_NFW_SOURCES = _nfw_json.loads(
    _nfw_zlib.decompress(_nfw_base64.b64decode("@@NFW_BUNDLE@@")).decode("utf-8"))


def _nfw_locate(name):
    base = name.replace(".", "/")
    if base + ".py" in _NFW_SOURCES:
        return base + ".py", False
    if base + "/__init__.py" in _NFW_SOURCES:
        return base + "/__init__.py", True
    return None


class _NfwImporter(_nfw_abc.MetaPathFinder, _nfw_abc.Loader):
    def find_spec(self, name, path=None, target=None):
        found = _nfw_locate(name)
        if found is None:
            return None
        return _nfw_util.spec_from_loader(name, self, origin="<nfw>/" + found[0],
                                          is_package=found[1])

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        relpath = _nfw_locate(module.__name__)[0]
        module.__file__ = "<nfw>/" + relpath
        code = compile(_NFW_SOURCES[relpath], "<nfw>/" + relpath, "exec", dont_inherit=True)
        exec(code, module.__dict__)

    def is_package(self, name):
        return _nfw_locate(name)[1]

    def get_source(self, name):
        return _NFW_SOURCES[_nfw_locate(name)[0]]


_nfw_sys.meta_path.insert(0, _NfwImporter())
'''


# ---------------------------------------------------------------- spec


def _size(value, name):
    if type(value) is not int or not 0 <= value <= MAX_SIZE:
        raise ValueError(f"{name} must be an integer from 0 to {MAX_SIZE}")
    return value


def _safe_file(root, relpath, what):
    """Resolve a repo-relative file path; refuse anything that leaves the repo root."""
    if (type(relpath) is not str or not relpath or "\\" in relpath or ":" in relpath
            or "\x00" in relpath or relpath.startswith("/")
            or any(part in ("", ".", "..") for part in relpath.split("/"))):
        raise ValueError(f"{what}: path must be relative, inside the repo, with no '..'")
    resolved = (root / PurePosixPath(relpath)).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{what}: path escapes the repository root")
    if not resolved.is_file():
        raise ValueError(f"{what}: {relpath} is not a file in the repository")
    return resolved


def _parse_ref(value, what):
    if type(value) is not str:
        raise ValueError(f"{what} must be a string like 'pkg/mod.py:function'")
    path, separator, name = value.rpartition(":")
    if (not separator or not path.endswith(".py") or not name.isidentifier()
            or keyword.iskeyword(name)):
        raise ValueError(f"{what} must look like 'pkg/mod.py:function', got {value!r}")
    return path, name


def _module_name(path, what):
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(part.isidentifier() and not keyword.iskeyword(part) for part in parts):
        raise ValueError(f"{what}: {path} does not map to an importable module name")
    return ".".join(parts)


def _function_node(source, name, positional, what):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError(f"{what}: file does not parse as Python ({type(exc).__name__})") from None
    found = None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            raise ValueError(f"{what}: {name} must be a plain def, not async")
        if isinstance(node, ast.FunctionDef) and node.name == name:
            found = node
    if found is None:
        raise ValueError(f"{what}: no top-level function named {name}")
    count = len(found.args.posonlyargs) + len(found.args.args)
    if count != positional:
        raise ValueError(f"{what}: {name} must take exactly {positional} positional "
                         f"parameter(s), it takes {count}")
    return found


def _read_source(path, what):
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{what}: {path.name} is over the {MAX_FILE_BYTES} byte per-file limit")
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{what}: {path.name} is not valid UTF-8") from None


def load_spec(root):
    """Validate root/nofreewins.json by parsing only; repo code is never imported."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("repo root is not a directory")
    path = _safe_file(root, "nofreewins.json", "spec")
    if path.stat().st_size > MAX_SPEC_BYTES:
        raise ValueError(f"nofreewins.json exceeds {MAX_SPEC_BYTES} bytes")
    try:
        raw = strict_json(path.read_bytes())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"nofreewins.json is not strict JSON: {exc}") from None
    if type(raw) is not dict:
        raise ValueError("nofreewins.json must be a JSON object")
    unknown = set(raw) - SPEC_KEYS
    missing = {"target", "generator", "timing_size"} - set(raw)
    if unknown or missing:
        raise ValueError(f"nofreewins.json keys: unknown {sorted(unknown)}, missing {sorted(missing)}")
    target_path, target_function = _parse_ref(raw["target"], "target")
    generator_path, generator_function = _parse_ref(raw["generator"], "generator")
    include = raw.get("include", "package")
    if include not in ("package", "full"):
        raise ValueError("include must be 'package' or 'full'")
    sizes = raw.get("correctness_sizes", DEFAULT_CORRECTNESS_SIZES)
    if type(sizes) is not list or not 1 <= len(sizes) <= 64:
        raise ValueError("correctness_sizes must be a list of 1 to 64 integers")
    sizes = [_size(value, "correctness_sizes entry") for value in sizes]
    description = raw.get("description", "")
    if type(description) is not str or len(description) > 4000:
        raise ValueError("description must be a string of at most 4000 characters")
    target_file = _safe_file(root, target_path, "target")
    generator_file = _safe_file(root, generator_path, "generator")
    _function_node(_read_source(target_file, "target"), target_function, 1, "target")
    _function_node(_read_source(generator_file, "generator"), generator_function, 2, "generator")
    return {"target": raw["target"], "generator": raw["generator"], "include": include,
            "timing_size": _size(raw["timing_size"], "timing_size"),
            "correctness_sizes": sizes, "description": description,
            "target_path": target_path, "target_function": target_function,
            "target_module": _module_name(target_path, "target"),
            "generator_path": generator_path, "generator_function": generator_function,
            "generator_module": _module_name(generator_path, "generator")}


def _repo_top_level_names(root):
    """Importable top-level names of the repo, from directory listing only."""
    names = set()
    for entry in root.iterdir():
        if entry.name in EXCLUDED_DIRS or entry.name.startswith("."):
            continue
        if entry.is_dir():
            names.add(entry.name)
        elif entry.name.endswith(".py"):
            names.add(entry.name[:-3])
    return names


def _check_generator(source, repo_names):
    if type(source) is not str or not source.strip():
        raise ValueError("generator_source must be a non-empty string")
    if len(source.encode("utf-8")) > GENERATOR_SOURCE_MAX:
        raise ValueError(f"generator_source is over the {GENERATOR_SOURCE_MAX} byte limit")
    _function_node(source, "generate_input", 2, "generator")  # parses; checks the signature
    for node in ast.walk(ast.parse(source)):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ValueError("generator: relative imports are not allowed")
            modules = [node.module or ""]
        for module in modules:
            top = module.split(".")[0]
            if top in FORBIDDEN_IMPORTS:
                raise ValueError(f"generator: import of {top} is not allowed")
            if top not in sys.stdlib_module_names and top not in repo_names:
                raise ValueError(f"generator: import of {top} is not allowed; only the standard "
                                 "library and the repo's own modules can be imported")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ValueError(f"generator: use of {node.id} is not allowed")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            raise ValueError(f"generator: use of .{node.attr} is not allowed")
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES | FORBIDDEN_ATTRS:
                    raise ValueError(f"generator: import of {alias.name} is not allowed")
        if (isinstance(node, ast.Constant) and type(node.value) is str
                and node.value in FORBIDDEN_ATTRS - {"modules"}):
            raise ValueError(f"generator: the string {node.value!r} is not allowed")


def _text_field(proposal, key, limit):
    value = proposal.get(key, "")
    if type(value) is not str or len(value) > limit:
        raise ValueError(f"{key} must be a string of at most {limit} characters")
    return value


def validate_proposal(root, proposal):
    """Validate an analyst proposal by parsing only; returns a spec shaped like load_spec's."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("repo root is not a directory")
    if type(proposal) is not dict:
        raise ValueError("proposal must be a JSON object")
    unknown = set(proposal) - PROPOSAL_KEYS
    missing = {"target", "generator_source"} - set(proposal)
    if unknown or missing:
        raise ValueError(f"proposal keys: unknown {sorted(unknown)}, missing {sorted(missing)}")
    target = proposal["target"]
    if type(target) is str and "." in target.rpartition(":")[2]:
        raise ValueError("target is a method; only top-level functions are supported "
                         f"for now, got {target!r}")
    target_path, target_function = _parse_ref(target, "target")
    if target_path.split("/")[0] == GENERATED_DIR:
        raise ValueError(f"target may not live in {GENERATED_DIR}/")
    include = proposal.get("include") or DEFAULTS["include"]
    if include not in ("package", "full"):
        raise ValueError("include must be 'package' or 'full'")
    timing_size = proposal.get("timing_size", DEFAULTS["timing_size"])
    if type(timing_size) is not int or not 1 <= timing_size <= MAX_SIZE:
        raise ValueError(f"timing_size must be an integer from 1 to {MAX_SIZE}")
    sizes = proposal.get("correctness_sizes", DEFAULTS["correctness_sizes"])
    if type(sizes) is not list or not 1 <= len(sizes) <= 64:
        raise ValueError("correctness_sizes must be a list of 1 to 64 integers")
    sizes = [_size(value, "correctness_sizes entry") for value in sizes]
    instruction = _text_field(proposal, "instruction", 4000)
    description = _text_field(proposal, "description", 2000)
    hints = proposal.get("hints", [])
    if (type(hints) is not list or len(hints) > 10 or
            not all(type(hint) is str and len(hint) <= 300 for hint in hints)):
        raise ValueError("hints must be a list of at most 10 strings of at most 300 characters")
    target_file = _safe_file(root, target_path, "target")
    _function_node(_read_source(target_file, "target"), target_function, 1, "target")
    _check_generator(proposal["generator_source"], _repo_top_level_names(root))
    return {"target": target, "generator": GENERATED_REF, "include": include,
            "timing_size": timing_size, "correctness_sizes": sizes,
            "description": description, "instruction": instruction, "hints": list(hints),
            "target_path": target_path, "target_function": target_function,
            "target_module": _module_name(target_path, "target"),
            "generator_path": f"{GENERATED_DIR}/gen.py", "generator_function": "generate_input",
            "generator_module": f"{GENERATED_DIR}.gen"}


# ---------------------------------------------------------------- fetching


def _disk_bytes(directory):
    total = 0
    for current, _, files in os.walk(directory):
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                pass
    return total


def _git_env():
    allowed = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR",
               "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1", LANG="C")
    return env


def _git(args, timeout=120):
    try:
        return subprocess.run(["git", *args], capture_output=True, timeout=timeout,
                              env=_git_env(), check=False)
    except FileNotFoundError:
        raise ValueError("git is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise ValueError(f"git timed out after {timeout} s") from None


def _clone(url, ref, destination):
    """Shallow-clone into a fresh dir; returns the commit. Never runs repo code."""
    command = ["clone", "--depth", "1", *(["--branch", ref] if ref else []), "--no-tags",
               "--", url, str(destination)]
    try:
        result = _git(command)
        if result.returncode != 0:
            tail = result.stderr.decode("utf-8", "replace").strip()[-300:]
            raise ValueError(f"git clone failed: {tail}")
        size = _disk_bytes(destination)
        if size > MAX_CLONE_BYTES:
            raise ValueError(f"clone is {size} bytes on disk, over the {MAX_CLONE_BYTES} byte limit")
        head = _git(["-C", str(destination), "rev-parse", "HEAD"], timeout=30)
        commit = head.stdout.decode("ascii", "replace").strip()
        if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ValueError("could not read the cloned commit")
        return commit, size
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def parse_github_url(repo):
    match = _GITHUB.fullmatch(repo)
    if match is None:
        raise ValueError("repo URL must be https://github.com/<owner>/<name>[.git][@<ref>]")
    owner, name, ref = match.groups()
    if ({owner, name} & {".", ".."} or (ref is not None and (
            ref[0] in "-/" or ref[-1] == "/" or ".." in ref or "//" in ref
            or ref.endswith(".lock")))):
        raise ValueError("repo URL has an unsafe owner, name or ref")
    return f"https://github.com/{owner}/{name}", ref


def fetch_repo(repo, dest_root="private/repos"):
    """Local directory in place, or a shallow clone of a public GitHub repo.

    A relative dest_root is anchored at the project root, not the working directory.
    """
    if type(repo) is not str or not repo:
        raise ValueError("repo must be a local directory or a GitHub URL")
    if "://" not in repo:
        path = Path(repo).resolve()
        if not path.is_dir():
            raise ValueError(f"repo path {repo} is not a directory")
        return path, {"kind": "local", "path": str(path)}
    url, ref = parse_github_url(repo)
    destination = ((PROJECT_ROOT / dest_root).resolve() /
                   f"{hashlib.sha256((url + (ref or '')).encode()).hexdigest()[:16]}-"
                   f"{uuid.uuid4().hex[:8]}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    commit, size = _clone(url, ref, destination)
    return destination, {"kind": "github", "url": url, "ref": ref, "commit": commit,
                         "clone_bytes": size}


# ---------------------------------------------------------------- bundle


def _walk_sources(root, include, packages=frozenset()):
    """Yield (relpath, file) for the .py files that ship, in walk order. Shared selection rules."""
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in EXCLUDED_DIRS)
        relative = Path(current).relative_to(root)
        for name in sorted(files):
            relpath = (relative / name).as_posix()
            if (not name.endswith(".py") or name.startswith("test_") or name.endswith("_test.py")
                    or (include == "package" and relpath.split("/")[0] not in packages)):
                continue
            try:
                file = _safe_file(root, relpath, "source")
            except ValueError:
                continue  # symlink out of the repo or not a regular file
            yield relpath, file


def collect_sources(root, spec, include):
    """relpath -> source for the .py files that ship to the sandbox."""
    if include not in ("package", "full"):
        raise ValueError("include must be 'package' or 'full'")
    root = Path(root).resolve()
    # A proposal's generator is written into the bundle later; it is not a file in the repo.
    required = {path for path in (spec["target_path"], spec["generator_path"])
                if path.split("/")[0] != GENERATED_DIR}
    packages = {path.split("/")[0] for path in required if "/" in path}
    sources, total = {}, 0

    def add(relpath, file):
        nonlocal total
        text = _read_source(file, relpath)
        total += len(text.encode("utf-8"))
        if total > MAX_SOURCE_BYTES:
            raise ValueError(f"repo sources exceed {MAX_SOURCE_BYTES} bytes; use include 'package'")
        sources[relpath] = text

    for relpath, file in _walk_sources(root, include, packages):
        add(relpath, file)
    for relpath in sorted(required - set(sources)):  # required files win over exclusions
        add(relpath, _safe_file(root, relpath, "required file"))
    return dict(sorted(sources.items()))


def _first_line(text, limit=120):
    for line in (text or "").strip().splitlines():
        return line.strip()[:limit]
    return ""


def repo_map(root, *, include="full", max_source_bytes=120_000):
    """Read-only overview of a repo for the analyst: files, function signatures, sources.

    Parses with ast only; nothing from the repo is imported or executed.
    """
    if include != "full":
        raise ValueError("repo_map supports include 'full' only")
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("repo root is not a directory")
    files, sources, used, truncated = [], {}, 0, False
    for relpath, file in sorted(_walk_sources(root, "full")):
        try:
            text = _read_source(file, relpath)
        except ValueError as exc:  # lenient: the map lists it, prepare still fails hard
            files.append({"path": relpath, "bytes": file.stat().st_size, "functions": [],
                          "unreadable": str(exc)})
            continue
        size = len(text.encode("utf-8"))
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            tree = ast.Module(body=[], type_ignores=[])
        functions = []

        def visit(body, prefix):
            for node in body:
                if isinstance(node, ast.FunctionDef):
                    functions.append({
                        "qualname": prefix + node.name,
                        "args": [a.arg for a in node.args.posonlyargs + node.args.args],
                        "lineno": node.lineno, "doc": _first_line(ast.get_docstring(node))})
                elif isinstance(node, ast.ClassDef):
                    visit(node.body, prefix + node.name + ".")

        visit(tree.body, "")
        files.append({"path": relpath, "bytes": size, "functions": functions})
        if not truncated and used + size <= max_source_bytes:
            sources[relpath] = text
            used += size
        else:
            truncated = True
    return {"files": files, "sources": sources, "truncated": truncated}


def _prelude(bundle):
    return PRELUDE.replace(_MARKER, bundle)


def build_bundle(sources):
    """(base64 of zlib(canonical json), sha256 of the canonical json)."""
    if (type(sources) is not dict or not sources or
            not all(type(k) is str and type(v) is str for k, v in sources.items())):
        raise ValueError("sources must be a non-empty dict of relpath -> source text")
    raw = canonical_json(sources)
    bundle = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    size = len(PRELUDE) - len(_MARKER) + len(bundle)
    if size > REPO_BUNDLE_MAX:
        raise ValueError(f"compressed repo bundle plus loader is {size} bytes, over the "
                         f"{REPO_BUNDLE_MAX} byte limit; use include 'package' or a smaller repo")
    return bundle, digest(raw)


def _decode_bundle(bundle):
    inflater = zlib.decompressobj()
    raw = inflater.decompress(base64.b64decode(bundle, validate=True), MAX_SOURCE_BYTES + (1 << 20))
    if inflater.unconsumed_tail:
        raise ValueError("sealed bundle decompresses past its size limit")
    return strict_json(raw)


def _worker_source(spec, bundle, module, function, alias, body):
    return (_prelude(bundle) + f"\nfrom {module} import {function} as {alias}\n\n" + body
            ).encode("utf-8")


def baseline_source(spec, bundle):
    return _worker_source(spec, bundle, spec["target_module"], spec["target_function"],
                          "_nfw_target", "def solve(data):\n    return _nfw_target(data)\n")


def generator_source(spec, bundle):
    return _worker_source(spec, bundle, spec["generator_module"], spec["generator_function"],
                          "_nfw_gen",
                          'def solve(req):\n    return _nfw_gen(req["seed"], req["size"])\n')


def _hoist_future(candidate):
    """`from __future__` imports must open the file, so lift them above the prelude."""
    try:
        text = candidate.decode("utf-8")
        body = ast.parse(text).body
    except (SyntaxError, ValueError, RecursionError):
        return b"", candidate
    lines, hoisted = io.StringIO(text, newline="").readlines(), []
    for node in body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            continue
        if not (isinstance(node, ast.ImportFrom) and node.module == "__future__"
                and node.level == 0):
            break
        segment = "".join(lines[node.lineno - 1:node.end_lineno])
        try:
            parsed = ast.parse(segment).body
        except SyntaxError:
            break
        if len(parsed) != 1 or ast.dump(parsed[0]) != ast.dump(node):
            break  # shares its lines with other statements; leave the candidate untouched
        hoisted.append(segment if segment.endswith(("\n", "\r")) else segment + "\n")
        for number in range(node.lineno - 1, node.end_lineno):
            lines[number] = "\n"  # keep later line numbers intact
    return "".join(hoisted).encode("utf-8"), "".join(lines).encode("utf-8")


def executed_source(candidate_bytes, bundle):
    head, body = _hoist_future(candidate_bytes)
    source = head + _prelude(bundle).encode("utf-8") + b"\n" + body
    if len(source) > SANDBOX_SOURCE_MAX:
        raise CandidateFailure("candidate_source_too_large")
    return source


# ---------------------------------------------------------------- preparation


def _derive(seed, label, index):
    return int(hashlib.sha256(f"{seed}:{label}:{index}".encode()).hexdigest()[:16], 16)


def _stderr_tail(result):
    try:
        raw = base64.b64decode(strict_json(result.proof_bytes)["stderr_b64"])
        return raw.decode("utf-8", "replace").strip()[-400:]
    except (ValueError, KeyError, TypeError):
        return ""


class _Limit(Exception):
    pass


class _Runner:
    """Runs preparation trials through the backend with the shared trial/proof checks."""

    def __init__(self, backend):
        self.backend, self.origin, self.count = backend, backend.origin, 0

    def trial(self, source, cases, timeout, what):
        trial_id = f"prep-{self.count:04d}"
        self.count += 1
        result = checked_trial(self.backend, self.origin, trial_id, source,
                               canonical_json({"cases": cases}), timeout, 180)
        if result.exit_code != 0 or result.failure_reason:
            if result.failure_reason in ("output_limit", "process_timeout"):
                raise _Limit(result.failure_reason)
            raise ValueError(f"{what} failed (exit code {result.exit_code}, "
                             f"{result.failure_reason or 'no reason'}): {_stderr_tail(result)}")
        try:
            return result, parse_results(result.stdout, [case["id"] for case in cases])
        except ValueError as exc:
            if "byte limit" in str(exc):
                raise _Limit("output_limit") from None
            raise ValueError(f"{what} produced unusable output: {exc}") from None

    def batched(self, source, cases, *, what, hint, limit=256, timeout=15):
        """Run cases in few trials; halve the batch when a trial hits a time/output limit.

        After each success the batch size follows the observed output size (up to the
        ceiling), so later trials stay under the 1 MiB output cap without failing first.
        """
        outputs, position, ceiling = {}, 0, 256
        while position < len(cases):
            batch, size = [], 0
            for case in cases[position:position + limit]:
                case_size = len(canonical_json(case))
                if batch and size + case_size > MAX_VALUE_BYTES:
                    break
                batch.append(case)
                size += case_size
            try:
                result, found = self.trial(source, batch, timeout, what)
            except _Limit as reason:
                if len(batch) == 1:
                    raise ValueError(f"{what} hit its {reason} limit on a single case "
                                     f"({batch[0]['id']}); {hint}") from None
                limit = ceiling = max(1, len(batch) // 2)
                continue
            outputs.update(found)
            position += len(batch)
            estimate = int(0.95 * MAX_VALUE_BYTES * len(batch) / max(1, len(result.stdout)))
            limit = max(1, min(ceiling, estimate))
        return outputs, limit


def _clip(value):
    return min(max(value, 1), 100_000)


def prepare_repo_session(session_id, repo, *, backend, include=None, proposal=None,
                         origin_info=None, root="private/sessions", repos_root="private/repos"):
    """Generate, run the baseline oracle on, and seal a repo task. Returns the public session.

    With `proposal` (analyst output) no nofreewins.json is read; `origin_info`, if given,
    replaces the fetch description (for a checkout the analyst already fetched).
    """
    from .sessions import _path
    path = _path(session_id, root)
    sealed = Path(root) / f"{session_id}.repo"
    if path.exists() or sealed.exists():
        raise ValueError("session already exists; choose a new session ID")
    origin = getattr(backend, "origin", None)
    if origin not in ("synthetic", "measured"):
        raise ValueError("backend must declare its trusted measurement origin")
    if origin == "measured" and not getattr(backend, "verified", False):
        from .backend import InfrastructureError
        raise InfrastructureError("Daytona must pass isolation probes first")
    if include not in (None, "package", "full"):
        raise ValueError("include must be 'package' or 'full'")
    repo_root, fetched_info = fetch_repo(repo, repos_root)
    if origin_info is None:
        origin_info = fetched_info
    elif type(origin_info) is not dict or len(canonical_json(origin_info)) > 4096:
        raise ValueError("origin_info must be a small JSON object")
    if proposal is None:
        spec, where = load_spec(repo_root), "in nofreewins.json"
    else:
        spec, where = validate_proposal(repo_root, proposal), "in the proposal"
    if include:
        spec["include"] = include
    sources = collect_sources(repo_root, spec, spec["include"])
    if proposal is not None:
        if any(name.split("/")[0] == GENERATED_DIR for name in sources):
            raise ValueError(f"the repo already has a {GENERATED_DIR} directory")
        sources[f"{GENERATED_DIR}/__init__.py"] = ""
        sources[f"{GENERATED_DIR}/gen.py"] = proposal["generator_source"]
        sources = dict(sorted(sources.items()))
    bundle, bundle_sha256 = build_bundle(sources)
    seed, runner = secrets.token_hex(16), _Runner(backend)
    sizes, timing_size = spec["correctness_sizes"], spec["timing_size"]

    def spec_case(label, prefix, index, size, seed_text):
        return {"id": f"{label}-{index}",
                "input": {"seed": _derive(seed_text, prefix, index), "size": size}}

    correctness = [spec_case("corr", "corr", i, sizes[i % len(sizes)], seed)
                   for i in range(CORRECTNESS_CASES)]
    public = [spec_case("public", "public", i, min(5, timing_size), "nofreewins-public")
              for i in range(2)]
    timing = {phase: [spec_case(phase, phase, i, timing_size, seed) for i in range(count)]
              for phase, count in PHASES}
    # Generation: seeds and sizes go in, JSON-safe inputs come out of the sandbox.
    generator = generator_source(spec, bundle)
    hint = f"lower timing_size or the correctness sizes {where}"
    generated, _ = runner.batched(generator, correctness + public, what="generator", hint=hint)
    produced, limit = runner.batched(generator, timing["warmup"], what="generator", hint=hint,
                                     limit=1)  # the first trial measures the input size
    generated.update(produced)
    for phase in ("screen", "acceptance"):
        produced, limit = runner.batched(generator, timing[phase], what="generator", hint=hint,
                                         limit=limit)
        generated.update(produced)
    for identifier, value in generated.items():
        if len(canonical_json(value)) > MAX_VALUE_BYTES:
            raise ValueError(f"generated input {identifier} exceeds 1 MiB; {hint}")

    def with_input(case):
        return {"id": case["id"], "input": generated[case["id"]]}

    cases = [with_input(case) for case in correctness]
    examples = [with_input(case) for case in public]
    workloads = {phase: [with_input(case) for case in group] for phase, group in timing.items()}
    # Oracle: the frozen baseline defines every expected output, timing workloads included.
    oracle = baseline_source(spec, bundle)
    # The oracle replays the harness's exact correctness batch plan (no halving), so every
    # batch evaluation will send is proven to run under the baseline within 15 s and 1 MiB.
    expected, first_batch = {}, None
    for batch in correctness_batches(cases):
        try:
            _, found = runner.trial(oracle, batch, 15, "baseline target")
        except _Limit as reason:
            raise ValueError(
                f"baseline target hit its {reason} limit on a correctness batch of "
                f"{len(batch)} cases (15 s, 1 MiB output); lower correctness_sizes "
                f"{where}") from None
        first_batch = first_batch or (batch, found)
        expected.update(found)
    produced, _ = runner.batched(oracle, examples, what="baseline target",
                                 hint="the baseline target must run on the public examples")
    expected.update(produced)
    durations = []
    for case in workloads["warmup"]:
        try:
            result, found = runner.trial(oracle, [case], 2, "baseline target")
        except _Limit as reason:
            raise ValueError(f"baseline target hit its {reason} limit (2 s per timing run) on a "
                             f"timing_size input; lower timing_size {where}") from None
        expected.update(found)
        durations.append(result.trusted_process_seconds)
    if median(durations) > MAX_BASELINE_SECONDS:
        raise ValueError(f"baseline median is {median(durations):.2f} s per timing run, over "
                         f"the {MAX_BASELINE_SECONDS} s limit (the harness caps runs at 2 s); "
                         f"lower timing_size {where}")
    for phase in ("screen", "acceptance"):
        produced, _ = runner.batched(oracle, workloads[phase], what="baseline target",
                                     hint=f"lower timing_size {where}")
        expected.update(produced)
    # Determinism: replay one correctness batch and the first timing workload.
    try:
        _, again = runner.trial(oracle, first_batch[0], 15, "baseline target")
        _, again_timing = runner.trial(oracle, [workloads["warmup"][0]], 2, "baseline target")
    except _Limit as reason:
        raise ValueError(f"baseline target hit its {reason} limit when re-run; "
                         "target output is not deterministic or timing is unstable") from None
    if (canonical_json(again) != canonical_json(first_batch[1]) or
            canonical_json(again_timing) != canonical_json(
                {key: expected[key] for key in again_timing})):
        raise ValueError("target output is not deterministic")
    # Seal: files first, the session file (exclusive create) last.
    files = {"spec.json": canonical_json(spec), "bundle.b64": bundle.encode("ascii"),
             "cases.json": canonical_json(cases), "expected.json": canonical_json(expected),
             "workloads.json": canonical_json(workloads),
             "public.json": canonical_json([{**case, "expected": expected[case["id"]]}
                                            for case in examples])}
    Path(root).mkdir(parents=True, exist_ok=True)
    sealed.mkdir(mode=0o700)
    try:
        for name, data in files.items():
            _write_private(sealed / name, data)
        data = {"schema_version": 1, "session_id": session_id, "seed": seed,
                "seed_sha256": digest(seed.encode()), "workload_lines": _clip(timing_size),
                "generator_sha256": digest(Path(__file__).read_bytes()),
                "repo": {"spec_sha256": digest(files["spec.json"]),
                         "bundle_sha256": bundle_sha256, "origin_info": origin_info,
                         "task_id": _task_id(bundle_sha256, digest(files["spec.json"])),
                         "files": {name: digest(blob) for name, blob in files.items()},
                         "include": spec["include"], "n_files": len(sources),
                         "source": "spec_file" if proposal is None else "proposal",
                         "bundle_bytes": len(PRELUDE) - len(_MARKER) + len(bundle),
                         "baseline_median_seconds": median(durations)}}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private(path, canonical_json(data))
    except BaseException as exc:
        # A failed seal leaves nothing behind (never remove a session file someone else won).
        shutil.rmtree(sealed, ignore_errors=True)
        if not isinstance(exc, FileExistsError):
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
        raise
    return {key: value for key, value in data.items() if key != "seed"}


def _write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- runtime task


def _clip_text(text, limit=2048):
    return text if len(text) <= limit else text[:limit] + f"... [truncated, {len(text)} chars]"


class RepoTask:
    """A sealed, digest-verified repo task presented through the harness task interface."""

    def __init__(self, session, spec, bundle, sources, sealed):
        self.session, self.spec, self.bundle, self.sources = session, spec, bundle, sources
        self._raw = sealed
        repo = session["repo"]
        self.task_id = repo["task_id"]
        self.baseline_source = baseline_source(spec, bundle)
        self._cases = strict_json(sealed["cases.json"])
        self._expected = strict_json(sealed["expected.json"])
        self._workloads = strict_json(sealed["workloads.json"])
        self._public = strict_json(sealed["public.json"])

    @classmethod
    def load(cls, session, root="private/sessions"):
        repo = session.get("repo") if type(session) is dict else None
        if type(repo) is not dict or type(repo.get("files")) is not dict:
            raise ValueError("session has no sealed repo task")
        if set(repo["files"]) != set(SEALED_FILES):
            raise ValueError("session lists unexpected sealed files")
        directory = Path(root) / f"{session['session_id']}.repo"
        sealed = {}
        for name in SEALED_FILES:
            try:
                data = (directory / name).read_bytes()
            except OSError:
                raise ValueError(f"sealed repo file {name} is missing") from None
            if digest(data) != repo["files"][name]:
                raise ValueError(f"sealed repo file {name} changed; use a new session")
            sealed[name] = data
        if digest(sealed["spec.json"]) != repo["spec_sha256"]:
            raise ValueError("sealed spec does not match the session")
        bundle = sealed["bundle.b64"].decode("ascii")
        sources = _decode_bundle(bundle)
        bundle_sha256 = digest(canonical_json(sources))
        if (bundle_sha256 != repo["bundle_sha256"] or
                repo["task_id"] != _task_id(bundle_sha256, repo["spec_sha256"])):
            raise ValueError("sealed bundle does not match the session")
        return cls(session, strict_json(sealed["spec.json"]), bundle, sources, sealed)

    def correctness_cases(self, seed=None, count=None, full_size=None):
        return self._cases if count is None else self._cases[:count]

    def expected_output(self, case):
        return self._expected[case["id"]]

    def workload(self, phase, index):
        if phase not in self._workloads or not 0 <= index < len(self._workloads[phase]):
            raise ValueError(f"no {phase} workload {index}")
        return self._workloads[phase][index]

    def timing_case(self, phase, index, seed, size, counter):
        return self.workload(phase, index)

    def executed_source(self, candidate):
        return executed_source(candidate, self.bundle)

    def artifacts(self):
        digests = canonical_json({"session_id": self.session["session_id"],
                                  "files": self.session["repo"]["files"]})
        return [("spec.json", self._raw["spec.json"]), ("bundle.b64", self._raw["bundle.b64"]),
                ("public_description.txt", self.public_summary().encode("utf-8")),
                ("sealed_files.json", digests)]

    def public_summary(self):
        spec = self.spec
        node = _function_node(self.sources[spec["target_path"]], spec["target_function"], 1,
                              "target")
        lines = self.sources[spec["target_path"]].splitlines()
        start = min([node.lineno] + [item.lineno for item in node.decorator_list])
        function = spec["target_function"]
        modules = sorted(name[:-3].replace("/", ".").removesuffix(".__init__")
                         for name in self.sources)
        instruction, hints = spec.get("instruction", ""), spec.get("hints", [])
        proposal = "instruction" in spec  # proposal specs always carry the key
        request = f"User request: {instruction}" if instruction else ""
        suggested = ("Suggested optimizations from analysis:\n"
                     + "\n".join(f"- {hint}" for hint in hints)) if hints else ""
        preserve = (f"Whatever the request, the suggestions or the analyst description say, "
                    f"{function}'s exact output must be preserved: the request, hints and "
                    "description guide HOW to optimize, never WHAT the output is."
                    if instruction or hints or proposal else "")
        parts = [f"Task: make {function} faster ({spec['target']}).", request,
                 ("Analyst description: " + spec["description"]
                  if proposal and spec["description"] else spec["description"]),
                 suggested, preserve,
                 f"Signature: {function}({ast.unparse(node.args)})",
                 "Full source of the function:\n" + "\n".join(lines[start - 1:node.end_lineno]),
                 f"Your module defines solve(data), and solve(data) must return exactly what "
                 f"{function}(data) returns for the same data.",
                 "Inputs and outputs are JSON-safe: only ints, strings, lists and dicts with "
                 "string keys, nested. No floats, booleans, None, tuples (use lists), sets or "
                 "custom objects. Output is capped at 1 MiB.",
                 "Standard library only; nothing is installed, and there is no network or "
                 "filesystem access.",
                 "Repo modules are importable from your solution, for example "
                 f"`from {spec['target_module']} import {function}`. Importable modules: "
                 + ", ".join(modules[:60]) + (" ..." if len(modules) > 60 else ""),
                 "Target: at least 3x faster than the baseline on median across hidden timing "
                 f"workloads (timing inputs have size {spec['timing_size']}).",
                 "Public examples (input -> expected output):"]
        for example in self._public:
            parts.append(_clip_text(canonical_json(example["input"]).decode("utf-8")) + " -> "
                         + _clip_text(canonical_json(example["expected"]).decode("utf-8")))
        parts.append("No special-casing of inputs, no clock patching, no early exit, "
                     "no lazy results.")
        return "\n\n".join(part for part in parts if part)
