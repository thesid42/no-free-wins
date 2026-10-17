import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import optimizer_core
from optimizer_core import repo_task
from optimizer_core.backend import TrialResult
from optimizer_core.canonical import canonical_json, digest, strict_json
from optimizer_core.harness import CandidateFailure, Harness
from optimizer_core.repo_task import (
    REPO_BUNDLE_MAX, RepoTask, baseline_source, build_bundle, collect_sources,
    executed_source, fetch_repo, generator_source, load_spec, parse_github_url,
    prepare_repo_session, repo_map, validate_proposal)
from optimizer_core.sessions import load_session
from optimizer_core import __main__ as cli

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "textstats"
DRIVER = Path(optimizer_core.__file__).with_name("remote_driver.py")

COUNTER_CANDIDATE = b'''import re
from collections import Counter

_WORD = re.compile(r"[a-z]+")


def solve(data):
    counts = Counter()
    for line in data["lines"]:
        counts.update(_WORD.findall(line.lower()))
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [[word, count] for word, count in ranked[:data["k"]]]
'''


class LocalSubprocessBackend:
    """Test-only: runs each trial's source in a local interpreter, like remote_driver does."""
    origin = "synthetic"
    environment_sha256 = digest(b"local-subprocess-test-runtime")

    def __init__(self):
        self.calls = []

    def run_trial(self, request):
        self.calls.append(request)
        with tempfile.TemporaryDirectory() as directory:
            solution = Path(directory) / "solution.py"
            solution.write_bytes(request.candidate_source)
            start = time.perf_counter()
            try:
                done = subprocess.run([sys.executable, "-I", str(DRIVER), str(solution)],
                                      input=request.input_bytes, capture_output=True,
                                      timeout=request.wall_timeout_seconds, cwd=directory)
                code, stdout, stderr, reason = done.returncode, done.stdout, done.stderr, ""
            except subprocess.TimeoutExpired:
                code, stdout, stderr, reason = -9, b"", b"", "process_timeout"
            seconds = max(time.perf_counter() - start, 1e-6)
        if len(stdout) + len(stderr) > 1 << 20:
            code, stdout, reason = -9, stdout[:1 << 20], "output_limit"
        proof = canonical_json({"origin": "synthetic", "trial_id": request.trial_id,
                                "stderr_b64": __import__("base64").b64encode(stderr[-2000:]).decode()})
        return TrialResult(request.trial_id, stdout, code, seconds, self.environment_sha256,
                           "local-process", digest(proof), True, proof_bytes=proof,
                           origin="synthetic", failure_reason=reason)


class CapBackend:
    """Fake backend: outputs a string of `size` chars per case; output_limit above `cap`."""
    origin = "synthetic"
    environment_sha256 = digest(b"cap-test-runtime")

    def __init__(self, cap):
        self.cap, self.batches = cap, []

    def run_trial(self, request):
        cases = strict_json(request.input_bytes)["cases"]
        self.batches.append(len(cases))
        wire = canonical_json({"results": [
            {"id": case["id"], "output": "x" * case["input"]["size"]} for case in cases]})
        proof = canonical_json({"trial_id": request.trial_id})
        over = len(wire) > self.cap
        return TrialResult(request.trial_id, wire[:self.cap], -9 if over else 0, 0.1,
                           self.environment_sha256, "fake", digest(proof), True,
                           proof_bytes=proof, origin="synthetic",
                           failure_reason="output_limit" if over else "")


def write_repo(directory, files, **spec):
    directory = Path(directory)
    for relpath, text in files.items():
        path = directory / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    config = {"target": "pkg/core.py:f", "generator": "pkg/gen.py:g", "timing_size": 10}
    config.update(spec)
    (directory / "nofreewins.json").write_text(json.dumps(config), encoding="utf-8")
    return directory


BASIC = {"pkg/__init__.py": "", "pkg/core.py": "def f(data):\n    return data\n",
         "pkg/gen.py": "def g(seed, size):\n    return [seed] * size\n"}


def run_source(source, cases):
    """Mimic remote_driver locally; returns the parsed envelope."""
    with tempfile.TemporaryDirectory() as directory:
        solution = Path(directory) / "solution.py"
        solution.write_bytes(source)
        done = subprocess.run([sys.executable, "-I", str(DRIVER), str(solution)],
                              input=canonical_json({"cases": cases}), capture_output=True,
                              cwd=directory, timeout=30)
    if done.returncode != 0:
        raise AssertionError(done.stderr.decode())
    return json.loads(done.stdout)


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def repo(self, files=BASIC, **spec):
        return write_repo(self.directory.name, files, **spec)

    def test_example_spec_loads(self):
        spec = load_spec(EXAMPLE)
        self.assertEqual(spec["target_module"], "textstats.core")
        self.assertEqual(spec["generator_function"], "generate_input")
        self.assertEqual(spec["include"], "package")
        self.assertEqual(spec["correctness_sizes"][-1], 1000)

    def test_defaults_and_minimal_spec(self):
        spec = load_spec(self.repo())
        self.assertEqual(spec["include"], "package")
        self.assertEqual(spec["correctness_sizes"], repo_task.DEFAULT_CORRECTNESS_SIZES)
        self.assertEqual(spec["description"], "")

    def test_bad_target_strings(self):
        for target in ("nocolon", "pkg/core.txt:f", "pkg/core.py:1bad", "pkg/core.py:", 5,
                       "pkg/core.py:class", "pkg/missing.py:f"):
            with self.assertRaises(ValueError, msg=repr(target)):
                load_spec(self.repo(target=target))

    def test_path_traversal_and_absolute_paths_rejected(self):
        outside = Path(self.directory.name).parent / "outside_nfw.py"
        outside.write_text("def f(data):\n    return data\n")
        self.addCleanup(outside.unlink)
        for target in ("../outside_nfw.py:f", "pkg/../../outside_nfw.py:f",
                       f"{outside}:f", "/etc/passwd.py:f", "pkg\\core.py:f"):
            with self.assertRaises(ValueError, msg=target):
                load_spec(self.repo(target=target))

    def test_symlink_escaping_root_rejected(self):
        repo = self.repo()
        outside = Path(self.directory.name).parent / "outside_link_nfw.py"
        outside.write_text("def f(data):\n    return data\n")
        self.addCleanup(outside.unlink)
        try:
            os.symlink(outside, repo / "pkg" / "link.py")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "escapes"):
            load_spec(self.repo(target="pkg/link.py:f"))

    def test_missing_function_and_wrong_arity(self):
        with self.assertRaisesRegex(ValueError, "no top-level function"):
            load_spec(self.repo(target="pkg/core.py:absent"))
        files = dict(BASIC, **{"pkg/core.py": "def f(a, b):\n    return a\n"})
        with self.assertRaisesRegex(ValueError, "exactly 1 positional"):
            load_spec(self.repo(files))
        files = dict(BASIC, **{"pkg/gen.py": "def g(seed):\n    return seed\n"})
        with self.assertRaisesRegex(ValueError, "exactly 2 positional"):
            load_spec(self.repo(files))
        files = dict(BASIC, **{"pkg/core.py": "async def f(data):\n    return data\n"})
        with self.assertRaisesRegex(ValueError, "async"):
            load_spec(self.repo(files))

    def test_unparsable_source_is_a_clear_error_and_never_executed(self):
        marker = Path(self.directory.name) / "executed"
        files = dict(BASIC, **{"pkg/core.py": f"open({str(marker)!r}, 'w')\ndef f(data:\n"})
        with self.assertRaisesRegex(ValueError, "does not parse"):
            load_spec(self.repo(files))
        files["pkg/core.py"] = f"open({str(marker)!r}, 'w')\ndef f(data):\n    return data\n"
        load_spec(self.repo(files))
        self.assertFalse(marker.exists())

    def test_size_and_type_validation(self):
        for bad in ({"timing_size": True}, {"timing_size": 1.5}, {"timing_size": -1},
                    {"timing_size": 10_000_001}, {"correctness_sizes": []},
                    {"correctness_sizes": [1, True]}, {"correctness_sizes": [1, -1]},
                    {"correctness_sizes": "1,2"}, {"include": "everything"},
                    {"description": 7}, {"surprise": 1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                load_spec(self.repo(**bad))

    def test_strict_json_enforced(self):
        repo = self.repo()
        (repo / "nofreewins.json").write_text(
            '{"target": "pkg/core.py:f", "target": "pkg/core.py:f", "generator": "x", "timing_size": 1}')
        with self.assertRaises(ValueError):
            load_spec(repo)


class CollectTests(unittest.TestCase):
    FILES = {
        "pkg/__init__.py": "", "pkg/core.py": "def f(data):\n    return data\n",
        "pkg/gen.py": "def g(seed, size):\n    return [seed] * size\n",
        "pkg/sub/__init__.py": "", "pkg/sub/leaf.py": "X = 1\n",
        "pkg/tests/test_core.py": "assert False\n", "pkg/test_inline.py": "assert False\n",
        "pkg/inline_test.py": "assert False\n", "pkg/notes.txt": "not python",
        "other/util.py": "Y = 2\n", "setup.py": "Z = 3\n",
        "tests/conftest.py": "assert False\n", "test/helper.py": "assert False\n",
        ".venv/lib.py": "assert False\n", "venv/lib.py": "assert False\n",
        "build/gen.py": "assert False\n", "dist/x.py": "assert False\n",
        "__pycache__/x.py": "assert False\n",
    }

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        write_repo(self.directory.name, self.FILES)
        self.spec = load_spec(self.directory.name)

    def test_package_mode_takes_only_the_target_package(self):
        sources = collect_sources(self.directory.name, self.spec, "package")
        self.assertEqual(list(sources), ["pkg/__init__.py", "pkg/core.py", "pkg/gen.py",
                                         "pkg/sub/__init__.py", "pkg/sub/leaf.py"])

    def test_full_mode_takes_everything_but_tests_and_build_dirs(self):
        sources = collect_sources(self.directory.name, self.spec, "full")
        self.assertEqual(list(sources), ["other/util.py", "pkg/__init__.py", "pkg/core.py",
                                         "pkg/gen.py", "pkg/sub/__init__.py",
                                         "pkg/sub/leaf.py", "setup.py"])

    def test_top_level_files_ship_alone_in_package_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            write_repo(directory, {"mod.py": "def f(data):\n    return data\n",
                                   "gen.py": "def g(seed, size):\n    return size\n",
                                   "extra.py": "pass\n", "pkg/x.py": "pass\n"},
                       target="mod.py:f", generator="gen.py:g")
            spec = load_spec(directory)
            self.assertEqual(list(collect_sources(directory, spec, "package")),
                             ["gen.py", "mod.py"])

    def test_required_files_are_kept_even_inside_excluded_dirs(self):
        with tempfile.TemporaryDirectory() as directory:
            write_repo(directory, {"pkg/core.py": "def f(data):\n    return data\n",
                                   "tests/gen.py": "def g(seed, size):\n    return size\n"},
                       generator="tests/gen.py:g")
            spec = load_spec(directory)
            self.assertIn("tests/gen.py", collect_sources(directory, spec, "package"))

    def test_non_utf8_source_rejected(self):
        (Path(self.directory.name) / "pkg" / "bad.py").write_bytes(b"x = '\xff'\n")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            collect_sources(self.directory.name, self.spec, "package")

    def test_invalid_include_rejected(self):
        with self.assertRaises(ValueError):
            collect_sources(self.directory.name, self.spec, "all")


class BundleTests(unittest.TestCase):
    def test_baseline_matches_calling_the_target_directly(self):
        spec = load_spec(EXAMPLE)
        bundle, sha = build_bundle(collect_sources(EXAMPLE, spec, "package"))
        self.assertEqual(len(sha), 64)
        module_spec = importlib.util.spec_from_file_location(
            "nfw_direct_core", EXAMPLE / "textstats" / "core.py")
        direct = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(direct)
        gen_spec = importlib.util.spec_from_file_location(
            "nfw_direct_gen", EXAMPLE / "textstats" / "gen.py")
        gen = importlib.util.module_from_spec(gen_spec)
        gen_spec.loader.exec_module(gen)
        inputs = [gen.generate_input(seed, size) for seed, size in ((1, 0), (2, 5), (3, 300))]
        cases = [{"id": f"c{i}", "input": value} for i, value in enumerate(inputs)]
        wire = run_source(baseline_source(spec, bundle), cases)
        self.assertEqual([row["output"] for row in wire["results"]],
                         [direct.top_words(value) for value in inputs])
        self.assertTrue(any(row["output"] for row in wire["results"]))

    def test_generator_source_matches_the_generator(self):
        spec = load_spec(EXAMPLE)
        bundle, _ = build_bundle(collect_sources(EXAMPLE, spec, "package"))
        gen_spec = importlib.util.spec_from_file_location(
            "nfw_direct_gen2", EXAMPLE / "textstats" / "gen.py")
        gen = importlib.util.module_from_spec(gen_spec)
        gen_spec.loader.exec_module(gen)
        wire = run_source(generator_source(spec, bundle),
                          [{"id": "g", "input": {"seed": 123456789012345, "size": 40}}])
        self.assertEqual(wire["results"][0]["output"], gen.generate_input(123456789012345, 40))

    def test_submodule_and_package_init_imports(self):
        files = {
            "pkg/__init__.py": "VALUE = 7\nfrom .sub import leaf\n",
            "pkg/sub/__init__.py": "NAME = 'sub'\n",
            "pkg/sub/leaf.py": ("from .. import VALUE\nfrom . import NAME\nimport pkg.core as c\n"
                                "def f(data):\n    return [VALUE, NAME, c.F, __name__, data]\n"),
            "pkg/core.py": "F = 'core'\n",
            "pkg/gen.py": "def g(seed, size):\n    return size\n"}
        with tempfile.TemporaryDirectory() as directory:
            write_repo(directory, files, target="pkg/sub/leaf.py:f")
            spec = load_spec(directory)
            self.assertEqual(spec["target_module"], "pkg.sub.leaf")
            bundle, _ = build_bundle(collect_sources(directory, spec, "package"))
        wire = run_source(baseline_source(spec, bundle), [{"id": "a", "input": 5}])
        self.assertEqual(wire["results"][0]["output"], [7, "sub", "core", "pkg.sub.leaf", 5])

    def test_candidate_can_import_repo_modules_and_future_imports_are_hoisted(self):
        spec = load_spec(EXAMPLE)
        bundle, _ = build_bundle(collect_sources(EXAMPLE, spec, "package"))
        candidate = (b'"""doc"""\nfrom __future__ import annotations\n'
                     b"from textstats.core import top_words\n"
                     b"def solve(data: dict) -> list:\n    return top_words(data)\n")
        source = executed_source(candidate, bundle)
        self.assertTrue(source.startswith(b"from __future__ import annotations\n"))
        data = {"lines": ["b a a", "C c"], "k": 2}
        wire = run_source(source, [{"id": "x", "input": data}])
        self.assertEqual(wire["results"][0]["output"], [["a", 2], ["c", 2]])

    def test_executed_source_without_future_is_prelude_newline_candidate(self):
        bundle, _ = build_bundle({"a.py": "x = 1\n"})
        self.assertEqual(executed_source(b"CAND\n", bundle),
                         repo_task._prelude(bundle).encode() + b"\nCAND\n")

    def test_bundle_digest_does_not_depend_on_compression(self):
        sources = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
        self.assertEqual(build_bundle(sources)[1], digest(canonical_json(sources)))

    def test_oversized_bundle_rejected_naming_the_limit(self):
        sources = {"big.py": "X = '" + os.urandom(200_000).hex() + "'\n"}
        with self.assertRaisesRegex(ValueError, str(REPO_BUNDLE_MAX)):
            build_bundle(sources)

    def test_executed_source_over_sandbox_limit_is_a_candidate_failure(self):
        bundle, _ = build_bundle({"a.py": "x = 1\n"})
        with self.assertRaises(CandidateFailure) as caught:
            executed_source(b"x = 1\n#" + b"x" * 262_144, bundle)
        self.assertEqual(str(caught.exception), "candidate_source_too_large")


class FetchTests(unittest.TestCase):
    def test_github_url_forms(self):
        self.assertEqual(parse_github_url("https://github.com/owner/name"),
                         ("https://github.com/owner/name", None))
        self.assertEqual(parse_github_url("https://github.com/owner/name.git"),
                         ("https://github.com/owner/name", None))
        self.assertEqual(parse_github_url("https://github.com/o-w_n.er/na.me.git@feature/x-1.2"),
                         ("https://github.com/o-w_n.er/na.me", "feature/x-1.2"))
        for bad in ("http://github.com/o/n", "https://gitlab.com/o/n", "https://github.com/o",
                    "https://github.com/o/n/extra", "https://u:p@github.com/o/n",
                    "https://github.com/o/n?x=1", "https://github.com/o/n@", "ssh://github.com/o/n",
                    "https://github.com/o/n@-evil", "https://github.com/o/n@a..b",
                    "https://github.com/o/n@a b", "https://github.com/../n",
                    "https://github.com/o/n@ref;rm", "https://github.com/o/n@/x",
                    "git@github.com:o/n.git", "file:///tmp/x"):
            with self.assertRaises(ValueError, msg=bad):
                parse_github_url(bad)

    def test_local_directory_is_used_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            path, info = fetch_repo(directory)
            self.assertEqual(path, Path(directory).resolve())
            self.assertEqual(info, {"kind": "local", "path": str(Path(directory).resolve())})
            with self.assertRaises(ValueError):
                fetch_repo(str(Path(directory) / "missing"))

    def test_github_fetch_builds_a_safe_clone_command(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if "clone" in argv:
                Path(argv[-1]).mkdir(parents=True)
                (Path(argv[-1]) / "x.py").write_text("pass\n")
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(argv, 0, b"a" * 40 + b"\n", b"")

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(subprocess, "run", fake_run):
            path, info = fetch_repo("https://github.com/o/n.git@v1", directory)
            self.assertEqual(path.parent, Path(directory).resolve())
            self.assertRegex(path.name, r"^[0-9a-f]{16}-[0-9a-f]{8}$")
        clone, kwargs = calls[0]
        self.assertEqual(clone[:6], ["git", "clone", "--depth", "1", "--branch", "v1"])
        self.assertIn("--no-tags", clone)
        self.assertEqual(clone[-3:-1], ["--", "https://github.com/o/n"])
        self.assertEqual(kwargs["timeout"], 120)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GIT_LFS_SKIP_SMUDGE"], "1")
        self.assertEqual(info["commit"], "a" * 40)
        self.assertEqual((info["kind"], info["ref"]), ("github", "v1"))

    def test_clone_failure_and_oversize_leave_nothing_behind(self):
        def failing(argv, **kwargs):
            Path(argv[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: repository not found")

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(subprocess, "run", failing):
            with self.assertRaisesRegex(ValueError, "repository not found"):
                fetch_repo("https://github.com/o/n", directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

        def big(argv, **kwargs):
            Path(argv[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(subprocess, "run", big), \
                mock.patch.object(repo_task, "MAX_CLONE_BYTES", -1):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                fetch_repo("https://github.com/o/n", directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_clone_of_a_local_git_repo_via_file_url(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            write_repo(source, BASIC)
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
            for command in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
                subprocess.run([*git, "-C", str(source), *command], check=True,
                               capture_output=True)
            head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
                                  capture_output=True).stdout.decode().strip()
            destination = Path(directory) / "clone"
            commit, size = repo_task._clone(source.as_uri(), None, destination)
            self.assertEqual(commit, head)
            self.assertTrue((destination / "pkg" / "core.py").is_file())
            self.assertEqual(load_spec(destination)["target_function"], "f")
            self.assertGreater(size, 0)


class BatchingTests(unittest.TestCase):
    def cases(self, count, size):
        return [{"id": f"c{i}", "input": {"seed": i, "size": size}} for i in range(count)]

    def test_batches_halve_on_output_limit_and_stay_under_the_cap(self):
        backend = CapBackend(cap=10_000)
        outputs, _ = repo_task._Runner(backend).batched(
            b"unused", self.cases(40, 1000), what="generator", hint="lower sizes")
        self.assertEqual(len(outputs), 40)
        self.assertEqual(backend.batches[:4], [40, 20, 10, 5])
        self.assertEqual(set(backend.batches[3:]), {5})

    def test_single_oversized_case_tells_the_user_what_to_lower(self):
        backend = CapBackend(cap=10_000)
        with self.assertRaisesRegex(ValueError, "lower timing_size"):
            repo_task._Runner(backend).batched(
                b"unused", self.cases(3, 20_000), what="generator",
                hint="lower timing_size or the correctness sizes")
        self.assertEqual(backend.batches, [3, 1])


class PreparedSessionTests(unittest.TestCase):
    """One prepared session on the local backend is shared by all tests in this class."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        cls.repo = base / "textstats"
        shutil.copytree(EXAMPLE, cls.repo, ignore=shutil.ignore_patterns("__pycache__"))
        config = json.loads((cls.repo / "nofreewins.json").read_text())
        config["timing_size"] = 1500
        (cls.repo / "nofreewins.json").write_text(json.dumps(config))
        cls.root = str(base / "sessions")
        cls.backend = LocalSubprocessBackend()
        cls.public = prepare_repo_session("repo-unit", str(cls.repo), backend=cls.backend,
                                          root=cls.root, repos_root=str(base / "repos"))
        cls.session = load_session("repo-unit", root=cls.root)
        cls.task = RepoTask.load(cls.session, root=cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_public_dict_has_no_seed_and_describes_the_repo(self):
        self.assertNotIn("seed", self.public)
        repo = self.public["repo"]
        self.assertEqual(repo["origin_info"]["kind"], "local")
        self.assertEqual(repo["task_id"],
                         repo_task._task_id(repo["bundle_sha256"], repo["spec_sha256"]))
        self.assertEqual(repo["include"], "package")
        self.assertEqual(repo["n_files"], 3)
        self.assertGreater(repo["baseline_median_seconds"], 0)
        self.assertEqual(self.public["workload_lines"], 1500)
        self.assertEqual(self.public["generator_sha256"],
                         digest(Path(repo_task.__file__).read_bytes()))
        self.assertEqual(set(repo["files"]), set(repo_task.SEALED_FILES))

    def test_sealed_files_are_private(self):
        sealed = Path(self.root) / "repo-unit.repo"
        self.assertEqual(stat.S_IMODE(sealed.stat().st_mode), 0o700)
        for name in repo_task.SEALED_FILES:
            self.assertEqual(stat.S_IMODE((sealed / name).stat().st_mode), 0o600, name)
        session = Path(self.root) / "repo-unit" / "session.json"
        self.assertEqual(stat.S_IMODE(session.stat().st_mode), 0o600)

    def test_every_trial_went_through_the_backend(self):
        self.assertTrue(all(call.trial_id.startswith("prep-") for call in self.backend.calls))
        self.assertTrue(all(len(call.candidate_source) <= 262_144 for call in self.backend.calls))

    def test_generated_and_oracle_data_shape(self):
        cases = list(self.task.correctness_cases())
        self.assertEqual(len(cases), 2000)
        sizes = self.public and load_spec(self.repo)["correctness_sizes"]
        for index in (0, 3, 1999):
            words = len([token for line in cases[index]["input"]["lines"]
                         for token in line.split() if token != "-"])
            self.assertEqual(cases[index]["id"], f"corr-{index}")
            self.assertEqual(words, sizes[index % len(sizes)])
        self.assertEqual(self.task.expected_output(cases[0]), [])  # size 0
        for phase, count in (("warmup", 5), ("screen", 5), ("acceptance", 30)):
            case = self.task.workload(phase, count - 1)
            self.assertEqual(case["id"], f"{phase}-{count - 1}")
            self.assertTrue(self.task.expected_output(case))
        with self.assertRaises(ValueError):
            self.task.workload("acceptance", 30)
        public = strict_json((Path(self.root) / "repo-unit.repo" / "public.json").read_bytes())
        self.assertEqual(len(public), 2)

    def test_oracle_is_the_baseline_on_the_stored_input(self):
        case = self.task.workload("screen", 2)
        wire = run_source(self.task.baseline_source, [case])
        self.assertEqual(wire["results"][0]["output"], self.task.expected_output(case))

    def test_digests_verified_and_tampering_detected(self):
        with tempfile.TemporaryDirectory() as copy:
            shutil.copytree(self.root, copy, dirs_exist_ok=True)
            RepoTask.load(load_session("repo-unit", root=copy), root=copy)
            for name in repo_task.SEALED_FILES:
                path = Path(copy) / "repo-unit.repo" / name
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaisesRegex(ValueError, name):
                    RepoTask.load(load_session("repo-unit", root=copy), root=copy)
                path.write_bytes(original)
            (Path(copy) / "repo-unit.repo" / "expected.json").unlink()
            with self.assertRaises(ValueError):
                RepoTask.load(load_session("repo-unit", root=copy), root=copy)

    def test_session_tampering_and_generator_commitment(self):
        with tempfile.TemporaryDirectory() as copy:
            shutil.copytree(self.root, copy, dirs_exist_ok=True)
            path = Path(copy) / "repo-unit" / "session.json"
            data = strict_json(path.read_bytes())
            data["generator_sha256"] = "0" * 64
            path.write_bytes(canonical_json(data))
            with self.assertRaises(ValueError):
                load_session("repo-unit", root=copy)
            data = strict_json(self.session_bytes())
            data["repo"]["bundle_sha256"] = "1" * 64
            path.write_bytes(canonical_json(data))
            with self.assertRaises(ValueError):
                RepoTask.load(data, root=copy)

    def session_bytes(self):
        return (Path(self.root) / "repo-unit" / "session.json").read_bytes()

    def test_existing_session_is_refused(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            prepare_repo_session("repo-unit", str(self.repo), backend=LocalSubprocessBackend(),
                                 root=self.root)

    def test_public_summary_and_artifacts(self):
        summary = self.task.public_summary()
        for needle in ("top_words(data)", "def top_words", "must return exactly what top_words",
                       "3x", "Standard library only", "textstats.core", "special-casing",
                       "clock patching", "early exit", "lazy results", "->",
                       "No floats, booleans"):
            self.assertIn(needle, summary)
        self.assertNotIn(self.session["seed"], summary)
        names = [name for name, _ in self.task.artifacts()]
        self.assertEqual(names[:2], ["spec.json", "bundle.b64"])
        self.assertIn("public_description.txt", names)

    def harness(self, backend, **kwargs):
        output = tempfile.TemporaryDirectory()
        self.addCleanup(output.cleanup)
        harness = Harness(backend, output=output.name, seed=self.session["seed"],
                          workload_lines=self.session["workload_lines"],
                          hidden_cases=20, task=self.task, **kwargs)
        return harness, Path(output.name)

    def request(self, source, mode="screen", **changes):
        value = {"schema_version": 1, "request_id": "r1", "session_id": "repo-unit",
                 "task_id": self.task.task_id,
                 "candidate": {"candidate_id": "c1", "source_utf8": source.decode(),
                               "sha256": digest(source)},
                 "mode": mode, "remaining_wall_seconds": 900,
                 "verifier": {"approved": True, "evidence_ids": ["fixture-verdict"]}}
        value.update(changes)
        return value

    def test_correct_candidate_screens(self):
        backend = LocalSubprocessBackend()
        harness, output = self.harness(backend)
        result = harness.evaluate_candidate(self.request(COUNTER_CANDIDATE))
        self.assertEqual(result["status"], "screened")
        self.assertTrue(result["correctness_passed"])
        self.assertGreater(result["screen_median_speedup"], 0)
        directory = output / result["evidence_ids"][0]
        self.assertEqual((directory / "candidate.py").read_bytes(), COUNTER_CANDIDATE)
        executed = (directory / "executed_candidate.py").read_bytes()
        self.assertTrue(executed.endswith(b"\n" + COUNTER_CANDIDATE))
        self.assertEqual(len(backend.calls), 1 + 10)  # 20 cases fit one batch, then 5 pairs
        self.assertEqual(backend.calls[-1].trial_id, "trial-0010")

    def test_acceptance_is_simulated_for_synthetic_origin(self):
        backend = LocalSubprocessBackend()
        harness, output = self.harness(backend)
        result = harness.evaluate_candidate(self.request(COUNTER_CANDIDATE, "acceptance"))
        self.assertEqual(result["status"], "simulated_acceptance")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["acceptance"]["pairs"], 30)
        self.assertEqual(len(backend.calls), 1 + 10 + 60)
        receipt = strict_json((output / result["receipt_id"] / "receipt.json").read_bytes())
        paths = {artifact["path"] for artifact in receipt["artifacts"]}
        self.assertTrue({"candidate.py", "executed_candidate.py", "baseline.py", "spec.json",
                         "bundle.b64", "public_description.txt", "sealed_files.json"} <= paths)
        self.assertNotIn("task_definition.py", paths)

    def test_wrong_candidate_is_rejected(self):
        harness, _ = self.harness(LocalSubprocessBackend())
        wrong = b"def solve(data):\n    return [['zzz', 1]]\n"
        result = harness.evaluate_candidate(self.request(wrong))
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["correctness_passed"])
        self.assertEqual(result["feedback_public"][0]["code"], "output_or_schema_mismatch")

    def test_crashing_candidate_is_rejected(self):
        harness, _ = self.harness(LocalSubprocessBackend())
        result = harness.evaluate_candidate(self.request(b"def solve(data):\n    return 1.5\n"))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["feedback_public"][0]["code"], "candidate_process_failed")

    def test_warmup_workloads_differ_from_acceptance_workloads(self):
        warmup = [self.task.workload("warmup", i) for i in range(5)]
        acceptance = [self.task.workload("acceptance", i) for i in range(30)]
        digests = {digest(canonical_json(case["input"])) for case in warmup + acceptance}
        self.assertEqual(len(digests), 35)
        self.assertFalse({case["id"] for case in warmup} & {case["id"] for case in acceptance})

    BIG_WRONG = COUNTER_CANDIDATE + (b'\n_ok = solve\n\ndef solve(data):\n'
                                    b'    return [] if len(data["lines"]) > 130 else _ok(data)\n')

    def test_candidate_wrong_only_on_timing_sized_inputs_is_rejected(self):
        small = max(len(case["input"]["lines"]) for case in self.task.correctness_cases())
        timing = [self.task.workload(phase, i)["input"]["lines"] for phase, n in
                  (("warmup", 5), ("screen", 5), ("acceptance", 30)) for i in range(n)]
        self.assertLess(small, 130)
        self.assertGreater(min(len(lines) for lines in timing), 130)
        for mode in ("screen", "acceptance"):
            harness, _ = self.harness(LocalSubprocessBackend())
            result = harness.evaluate_candidate(self.request(self.BIG_WRONG, mode))
            self.assertEqual(result["status"], "rejected", mode)
            self.assertTrue(result["correctness_passed"], mode)
            self.assertEqual(result["feedback_public"][0]["code"], "output_or_schema_mismatch")
            self.assertFalse(result["accepted"])

    def test_task_id_mismatch_is_rejected(self):
        harness, _ = self.harness(LocalSubprocessBackend())
        with self.assertRaises(ValueError):
            harness.evaluate_candidate(self.request(COUNTER_CANDIDATE, task_id="logs-parse-v1"))

    def test_policy_findings_run_on_the_worker_source(self):
        backend = LocalSubprocessBackend()
        harness, _ = self.harness(backend)
        cheat = COUNTER_CANDIDATE + b"\nimport time\ntime.perf_counter = lambda: 0\n"
        result = harness.evaluate_candidate(self.request(cheat))
        self.assertEqual(result["feedback_public"][0]["code"], "policy_clock_patch")
        self.assertEqual(backend.calls, [])

    def test_oversized_executed_source_is_a_candidate_rejection(self):
        backend = LocalSubprocessBackend()
        harness, _ = self.harness(backend)
        source = b"def solve(data):\n    return []\n#" + b"x" * (262_144 - 32)
        self.assertEqual(len(source), 262_144)
        result = harness.evaluate_candidate(self.request(source))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["feedback_public"][0]["code"], "candidate_source_too_large")
        self.assertEqual(backend.calls, [])


class TinyRepoTests(unittest.TestCase):
    """Small repos on the local backend for prepare-time guards."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def prepare(self, session_id="tiny", files=BASIC, backend=None, name="repo", **spec):
        spec.setdefault("correctness_sizes", [0, 1, 2])
        repo = write_repo(self.base / name, files, **spec)
        return prepare_repo_session(session_id, str(repo), backend=backend or
                                    LocalSubprocessBackend(), root=str(self.base / "sessions"),
                                    repos_root=str(self.base / "repos"))

    def test_oracle_replays_the_harness_batch_plan(self):
        files = dict(BASIC, **{"pkg/core.py": "def f(data):\n    return [data] * 4\n",
                               "pkg/gen.py": "def g(seed, size):\n    return [1] * size\n"})
        with self.assertRaisesRegex(ValueError, "lower correctness_sizes"):
            self.prepare(files=files, correctness_sizes=[1000])
        self.assertFalse((self.base / "sessions" / "tiny.repo").exists())
        # The same target is fine when the planned batches fit under the output cap.
        self.prepare("fits", files=files, correctness_sizes=[10])

    def test_batch_plan_is_shared_with_the_harness(self):
        from optimizer_core.harness import correctness_batches
        cases = [{"id": str(i), "input": "x" * 100} for i in range(600)]
        self.assertEqual([len(batch) for batch in correctness_batches(cases)], [256, 256, 88])
        cases = [{"id": str(i), "input": "x" * 400_000} for i in range(5)]
        self.assertEqual([len(batch) for batch in correctness_batches(cases)], [2, 2, 1])

    def test_task_id_binds_the_spec_as_well_as_the_bundle(self):
        first = self.prepare("one", description="first")
        second = self.prepare("two", name="repo2", description="second")
        self.assertEqual(first["repo"]["bundle_sha256"], second["repo"]["bundle_sha256"])
        self.assertNotEqual(first["repo"]["task_id"], second["repo"]["task_id"])
        session = load_session("one", root=str(self.base / "sessions"))
        session["repo"]["spec_sha256"] = second["repo"]["spec_sha256"]
        with self.assertRaises(ValueError):
            RepoTask.load(session, root=str(self.base / "sessions"))

    def test_failed_seal_leaves_nothing_behind(self):
        real = repo_task._write_private

        def failing(path, data):
            if Path(path).name == "public.json":
                raise OSError("disk full")
            return real(path, data)

        with mock.patch.object(repo_task, "_write_private", failing):
            with self.assertRaises(OSError):
                self.prepare()
        sessions = self.base / "sessions"
        self.assertEqual(list(sessions.iterdir()), [])
        self.prepare()  # the ID is reusable
        self.assertTrue((sessions / "tiny" / "session.json").is_file())

    def test_nondeterministic_target_rejected(self):
        files = dict(BASIC, **{"pkg/core.py": ("import random\n\ndef f(data):\n"
                                               "    return [random.randrange(10**9)]\n")})
        with self.assertRaisesRegex(ValueError, "not deterministic"):
            self.prepare(files=files)
        self.assertFalse((self.base / "sessions").exists())

    def test_slow_baseline_rejected_with_headroom_under_the_2s_cap(self):
        import dataclasses

        class Slow(LocalSubprocessBackend):
            def run_trial(self, request):
                result = super().run_trial(request)
                return dataclasses.replace(result, trusted_process_seconds=1.5)

        with self.assertRaisesRegex(ValueError, "lower timing_size"):
            self.prepare(backend=Slow())

    def test_per_file_size_cap(self):
        repo = write_repo(self.base / "big", BASIC)
        (repo / "pkg" / "big.py").write_text("#" + "x" * (1 << 20) + "\n")
        with self.assertRaisesRegex(ValueError, "per-file limit"):
            collect_sources(repo, load_spec(repo), "package")
        (repo / "pkg" / "core.py").write_text("#" + "x" * (1 << 20) + "\n")
        with self.assertRaisesRegex(ValueError, "per-file limit"):
            load_spec(repo)


GOOD_GENERATOR = """import random


def generate_input(seed, size):
    rng = random.Random(seed)
    words = ["alpha", "beta", "Gamma", "delta", "eps", "zeta", "eta", "theta"]
    lines, remaining = [], size
    while remaining > 0:
        count = min(remaining, rng.randrange(3, 9))
        lines.append(" ".join(rng.choice(words) + rng.choice(["", ",", "7"])
                              for _ in range(count)))
        remaining -= count
    return {"lines": lines, "k": 10}
"""

MAP_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": ('"""Core."""\n\ndef parse(data, flag):\n    """Parse it.\n\n    More."""\n    return data\n\n'
                    "class Box:\n    def put(self, item):\n        return item\n\n"
                    "    class Inner:\n        def deep(self):\n            pass\n"),
    "pkg/bad.py": "def broken(:\n",
    "tests/test_x.py": "def test_x():\n    pass\n",
    "boom.py": "raise SystemExit(3)\n",
}


class RepoMapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_textstats_example(self):
        mapped = repo_map(EXAMPLE)
        paths = [entry["path"] for entry in mapped["files"]]
        self.assertEqual(paths, sorted(paths))
        self.assertIn("textstats/core.py", paths)
        core = next(e for e in mapped["files"] if e["path"] == "textstats/core.py")
        top = next(f for f in core["functions"] if f["qualname"] == "top_words")
        self.assertEqual(top["args"], ["data"])
        self.assertGreater(top["lineno"], 0)
        self.assertEqual(core["bytes"], len(mapped["sources"]["textstats/core.py"].encode()))
        self.assertFalse(mapped["truncated"])
        self.assertEqual(set(mapped["sources"]), set(paths))

    def test_signatures_methods_exclusions_and_no_execution(self):
        marker = Path(self.directory.name) / "executed"
        files = dict(MAP_FILES, **{"side.py": f"open({str(marker)!r}, 'w')\n"})
        write_repo(self.directory.name, files)
        mapped = repo_map(self.directory.name)
        by_path = {entry["path"]: entry for entry in mapped["files"]}
        self.assertNotIn("tests/test_x.py", by_path)
        self.assertEqual(list(by_path), sorted(by_path))
        functions = {f["qualname"]: f for f in by_path["pkg/core.py"]["functions"]}
        self.assertEqual(list(functions), ["parse", "Box.put", "Box.Inner.deep"])
        self.assertEqual(functions["parse"]["args"], ["data", "flag"])
        self.assertEqual(functions["parse"]["doc"], "Parse it.")
        self.assertEqual(functions["parse"]["lineno"], 3)
        self.assertEqual(functions["Box.put"]["args"], ["self", "item"])
        self.assertEqual(by_path["pkg/bad.py"]["functions"], [])  # unparsable: listed, no functions
        self.assertEqual(by_path["boom.py"]["functions"], [])      # SystemExit never ran
        self.assertFalse(marker.exists())

    def test_doc_is_one_short_line(self):
        write_repo(self.directory.name, {"m.py": 'def f(x):\n    """' + "a" * 300 + '\n    b"""\n'})
        mapped = repo_map(self.directory.name)
        self.assertEqual(len(mapped["files"][0]["functions"][0]["doc"]), 120)

    def test_truncation_keeps_path_order_prefix(self):
        write_repo(self.directory.name, {"a.py": "x = 1\n" * 10, "b.py": "y = 1\n" * 10,
                                         "c.py": "z = 1\n"})
        mapped = repo_map(self.directory.name, max_source_bytes=70)
        self.assertEqual([e["path"] for e in mapped["files"]], ["a.py", "b.py", "c.py"])
        self.assertEqual(list(mapped["sources"]), ["a.py"])  # b does not fit; c is not skipped to
        self.assertTrue(mapped["truncated"])
        self.assertFalse(repo_map(self.directory.name)["truncated"])

    def test_limits_and_modes(self):
        write_repo(self.directory.name, BASIC)
        with self.assertRaises(ValueError):
            repo_map(self.directory.name, include="package")
        with self.assertRaises(ValueError):
            repo_map(Path(self.directory.name) / "missing")

    def test_unreadable_files_are_listed_not_fatal(self):
        write_repo(self.directory.name, BASIC)
        pkg = Path(self.directory.name) / "pkg"
        (pkg / "huge.py").write_text("#" + "x" * (1 << 20))
        (pkg / "latin.py").write_bytes(b"x = '\xff'\n")
        mapped = repo_map(self.directory.name)
        by_path = {entry["path"]: entry for entry in mapped["files"]}
        self.assertEqual(list(by_path), sorted(by_path))
        self.assertIn("per-file limit", by_path["pkg/huge.py"]["unreadable"])
        self.assertIn("UTF-8", by_path["pkg/latin.py"]["unreadable"])
        for name in ("pkg/huge.py", "pkg/latin.py"):
            self.assertEqual(by_path[name]["functions"], [])
            self.assertNotIn(name, mapped["sources"])
        self.assertNotIn("unreadable", by_path["pkg/core.py"])
        self.assertIn("pkg/core.py", mapped["sources"])
        # collect_sources and prepare still fail hard on the same files.
        with self.assertRaises(ValueError):
            collect_sources(self.directory.name, load_spec(self.directory.name), "full")


class ProposalValidationTests(unittest.TestCase):
    FILES = {"pkg/__init__.py": "", "pkg/core.py": "def f(data):\n    return data\n\n"
             "def two(a, b):\n    return a\n\nclass K:\n    def m(self):\n        pass\n",
             "helper.py": "def make(n):\n    return [n]\n"}

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        write_repo(self.directory.name, self.FILES)
        (Path(self.directory.name) / "nofreewins.json").unlink()

    def proposal(self, **changes):
        value = {"target": "pkg/core.py:f", "generator_source": GOOD_GENERATOR,
                 "timing_size": 100, "correctness_sizes": [0, 1, 5], "include": "package",
                 "instruction": "make it fast", "hints": ["use a Counter"],
                 "description": "returns data"}
        value.update(changes)
        return value

    def check(self, **changes):
        return validate_proposal(self.directory.name, self.proposal(**changes))

    def test_good_proposal_has_the_load_spec_shape(self):
        spec = self.check()
        self.assertEqual(spec["generator"], "_nfw_generated/gen.py:generate_input")
        self.assertEqual(spec["target_module"], "pkg.core")
        self.assertEqual(spec["generator_module"], "_nfw_generated.gen")
        self.assertEqual((spec["generator_path"], spec["generator_function"]),
                         ("_nfw_generated/gen.py", "generate_input"))
        self.assertEqual((spec["timing_size"], spec["correctness_sizes"], spec["include"]),
                         (100, [0, 1, 5], "package"))
        self.assertEqual((spec["instruction"], spec["hints"], spec["description"]),
                         ("make it fast", ["use a Counter"], "returns data"))
        shape = set(load_spec(EXAMPLE)) | {"instruction", "hints"}
        self.assertEqual(set(spec), shape)

    def test_defaults_fill_in_omitted_fields(self):
        proposal = {"target": "pkg/core.py:f", "generator_source": GOOD_GENERATOR}
        spec = validate_proposal(self.directory.name, proposal)
        self.assertEqual({key: spec[key] for key in repo_task.DEFAULTS}, repo_task.DEFAULTS)
        self.assertEqual((spec["instruction"], spec["hints"], spec["description"]), ("", [], ""))

    def test_target_problems(self):
        for target, message in (("pkg/core.py:absent", "no top-level function"),
                                ("pkg/core.py:K.m", "method"),
                                ("pkg/core.py:two", "exactly 1 positional"),
                                ("pkg/missing.py:f", "not a file"),
                                ("../x.py:f", "relative"),
                                ("nocolon", "pkg/mod.py:function"),
                                ("_nfw_generated/gen.py:f", "_nfw_generated")):
            with self.assertRaisesRegex(ValueError, message, msg=target):
                self.check(target=target)

    def test_generator_problems(self):
        bad = {
            "import os\ndef generate_input(seed, size):\n    return size\n": "import of os",
            "import subprocess\ndef generate_input(seed, size):\n    return size\n": "subprocess",
            "from os import path\ndef generate_input(seed, size):\n    return size\n": "os",
            "import os.path\ndef generate_input(seed, size):\n    return size\n": "os",
            "import socket, ctypes\ndef generate_input(seed, size):\n    return size\n": "socket",
            "import importlib\ndef generate_input(seed, size):\n    return size\n": "importlib",
            "import builtins\ndef generate_input(seed, size):\n    return size\n": "builtins",
            "import numpy\ndef generate_input(seed, size):\n    return size\n": "numpy",
            "from . import x\ndef generate_input(seed, size):\n    return size\n": "relative",
            "def generate_input(seed, size):\n    return open('x').read()\n": "open",
            "def generate_input(seed, size):\n    return eval('1')\n": "eval",
            "def generate_input(seed, size):\n    exec('x=1')\n    return 1\n": "exec",
            "def generate_input(seed, size):\n    return __import__('os')\n": "__import__",
            "def generate_input(seed, size):\n    return compile('1', 'x', 'eval')\n": "compile",
            "def make(seed, size):\n    return size\n": "no top-level function",
            "def generate_input(seed):\n    return seed\n": "exactly 2 positional",
            "def generate_input(seed, size, extra):\n    return seed\n": "exactly 2 positional",
            "def generate_input(seed, size:\n": "does not parse",
            "": "non-empty",
        }
        for source, message in bad.items():
            with self.assertRaisesRegex(ValueError, message, msg=source):
                self.check(generator_source=source)
        with self.assertRaises(ValueError):
            self.check(generator_source=5)

    def test_generator_bypass_attempts_are_rejected(self):
        body = "def generate_input(seed, size):\n    return size\n"
        bad = {
            "from io import open as o\n" + body: "io",
            "import posix\n" + body: "posix",
            "import nt\n" + body: "nt",
            "import sys\n" + body: "sys",
            "from sys import modules\n" + body: "sys",
            "import pathlib\n" + body: "pathlib",
            "import pickle\n" + body: "pickle",
            "import threading\n" + body: "threading",
            "import tempfile\n" + body: "tempfile",
            "import signal\n" + body: "signal",
            "import multiprocessing\n" + body: "multiprocessing",
            "import random\nfrom random import __import__ as q\n" + body: "__import__",
            "import random\ndef generate_input(seed, size):\n"
            "    return random.__builtins__['open']\n": "__builtins__",
            "import random\ndef generate_input(seed, size):\n"
            "    return getattr(random, '__builtins__')\n": "getattr",
            "import random\ndef generate_input(seed, size):\n"
            "    return random.__dict__['_os'].modules\n": "modules",
            "def generate_input(seed, size):\n    return generate_input.__globals__\n":
                "__globals__",
            "def generate_input(seed, size):\n    return generate_input.__code__\n":
                "__code__",
            "def generate_input(seed, size):\n    return ().__class__.__subclasses__()\n":
                "__subclasses__",
            "def generate_input(seed, size):\n    return globals()\n": "globals",
            "x = '__builtins__'\n" + body: "__builtins__",
        }
        for source, message in bad.items():
            with self.assertRaisesRegex(ValueError, message, msg=source):
                self.check(generator_source=source)

    def test_generator_words_that_look_like_names_are_fine(self):
        source = ("import re\nWORDS = ['open', 'compile', 'modules']\n_R = re.compile('a')\n"
                  "def generate_input(seed, size):\n    return WORDS[size % 3]\n")
        self.check(generator_source=source)

    def test_generator_imports_that_are_allowed(self):
        for source in (GOOD_GENERATOR,
                       "import random, math, string\nfrom collections import Counter\n"
                       "def generate_input(seed, size):\n    return size\n",
                       "import helper\nfrom pkg.core import f\nimport pkg\n"
                       "def generate_input(seed, size):\n    return helper.make(size)\n"):
            self.check(generator_source=source)

    def test_generator_oversize(self):
        source = "# " + "x" * 33_000 + "\ndef generate_input(seed, size):\n    return size\n"
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.check(generator_source=source)

    def test_size_text_and_key_limits(self):
        for changes in ({"timing_size": 0}, {"timing_size": 10_000_001}, {"timing_size": True},
                        {"timing_size": 1.5}, {"correctness_sizes": []},
                        {"correctness_sizes": [1] * 65}, {"correctness_sizes": [-1]},
                        {"correctness_sizes": [10_000_001]}, {"correctness_sizes": [True]},
                        {"include": "everything"}, {"instruction": "x" * 4001},
                        {"instruction": 5}, {"description": "x" * 2001},
                        {"hints": ["x"] * 11}, {"hints": ["x" * 301]}, {"hints": "one"},
                        {"hints": [3]}, {"surprise": 1}):
            with self.assertRaises(ValueError, msg=str(changes)):
                self.check(**changes)
        self.check(timing_size=1, correctness_sizes=[0] * 64, instruction="x" * 4000,
                   description="x" * 2000, hints=["x" * 300] * 10)
        with self.assertRaises(ValueError):
            validate_proposal(self.directory.name, {"target": "pkg/core.py:f"})
        with self.assertRaises(ValueError):
            validate_proposal(self.directory.name, [])

    def test_validation_never_runs_repo_or_generator_code(self):
        marker = Path(self.directory.name) / "ran"
        (Path(self.directory.name) / "pkg" / "core.py").write_text(
            f"open({str(marker)!r}, 'w')\ndef f(data):\n    return data\n")
        source = (f"open({str(marker)!r}, 'w')\ndef generate_input(seed, size):\n"
                  "    return size\n")
        with self.assertRaisesRegex(ValueError, "open"):
            self.check(generator_source=source)
        self.check(generator_source=source.replace("open(", "str(", 1))
        self.assertFalse(marker.exists())


class ProposalSessionTests(unittest.TestCase):
    """A textstats copy with nofreewins.json deleted, sealed from an analyst proposal."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        cls.repo = base / "textstats"
        shutil.copytree(EXAMPLE, cls.repo, ignore=shutil.ignore_patterns("__pycache__"))
        config = json.loads((cls.repo / "nofreewins.json").read_text())
        config["timing_size"] = 1500
        (cls.repo / "nofreewins.json").write_text(json.dumps(config))
        cls.root = str(base / "sessions")
        cls.spec_public = prepare_repo_session(
            "repo-spec", str(cls.repo), backend=LocalSubprocessBackend(), root=cls.root,
            repos_root=str(base / "repos"))
        (cls.repo / "nofreewins.json").unlink()
        cls.proposal = {
            "target": "textstats/core.py:top_words", "generator_source": GOOD_GENERATOR,
            "timing_size": 1500, "correctness_sizes": [0, 1, 2, 3, 5, 8, 13, 50, 200],
            "include": "package", "instruction": "Avoid per-word Python loops.",
            "hints": ["Use collections.Counter", "Compile the regex once"],
            "description": "Top k words, case-insensitive."}
        cls.backend = LocalSubprocessBackend()
        cls.public = prepare_repo_session(
            "repo-prop", str(cls.repo), backend=cls.backend, proposal=cls.proposal,
            origin_info={"kind": "github", "url": "https://github.com/o/n", "commit": "a" * 40},
            root=cls.root, repos_root=str(base / "repos"))
        cls.session = load_session("repo-prop", root=cls.root)
        cls.task = RepoTask.load(cls.session, root=cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_sealed_with_generated_sources_and_provenance(self):
        repo = self.public["repo"]
        self.assertEqual(repo["source"], "proposal")
        self.assertEqual(self.spec_public["repo"]["source"], "spec_file")
        self.assertEqual(repo["origin_info"]["kind"], "github")
        self.assertEqual(repo["n_files"], 5)  # 3 textstats files + _nfw_generated x2
        self.assertEqual(self.task.sources["_nfw_generated/gen.py"], GOOD_GENERATOR)
        self.assertEqual(self.task.sources["_nfw_generated/__init__.py"], "")
        self.assertEqual(repo["task_id"],
                         repo_task._task_id(repo["bundle_sha256"], repo["spec_sha256"]))
        self.assertEqual(self.task.spec["generator"], "_nfw_generated/gen.py:generate_input")
        self.assertEqual(self.task.spec["instruction"], "Avoid per-word Python loops.")
        self.assertEqual(self.task.spec["hints"], self.proposal["hints"])
        self.assertEqual(self.public["workload_lines"], 1500)

    def test_task_id_differs_from_the_spec_file_session(self):
        self.assertNotEqual(self.public["repo"]["task_id"], self.spec_public["repo"]["task_id"])
        self.assertNotEqual(self.public["repo"]["bundle_sha256"],
                            self.spec_public["repo"]["bundle_sha256"])

    def test_task_id_covers_the_instruction_hints_and_generator(self):
        base = Path(self.directory.name)
        ids = {self.public["repo"]["task_id"]}
        for index, changes in enumerate(({"instruction": "different"}, {"hints": []},
                                         {"generator_source": GOOD_GENERATOR + "# note\n"})):
            public = prepare_repo_session(
                f"repo-var{index}", str(self.repo), backend=LocalSubprocessBackend(),
                proposal={**self.proposal, **changes}, root=self.root,
                repos_root=str(base / "repos"))
            ids.add(public["repo"]["task_id"])
        self.assertEqual(len(ids), 4)

    def test_public_summary_has_request_hints_and_output_guard(self):
        summary = self.task.public_summary()
        self.assertIn("User request: Avoid per-word Python loops.", summary)
        self.assertIn("Suggested optimizations from analysis:\n- Use collections.Counter\n"
                      "- Compile the regex once", summary)
        self.assertIn("Analyst description: Top k words, case-insensitive.", summary)
        self.assertIn("exact output must be preserved", summary)
        self.assertIn("the request, the suggestions or the analyst description", summary)
        self.assertIn("never WHAT the output is", summary)
        self.assertIn("must return exactly what top_words", summary)
        spec_summary = RepoTask.load(
            load_session("repo-spec", root=self.root), root=self.root).public_summary()
        self.assertNotIn("User request", spec_summary)
        self.assertNotIn("Analyst description", spec_summary)

    def test_generated_inputs_come_from_the_analyst_generator(self):
        cases = list(self.task.correctness_cases())
        self.assertEqual(len(cases), 2000)
        self.assertEqual(cases[0]["input"], {"lines": [], "k": 10})
        self.assertEqual(self.task.expected_output(cases[0]), [])
        self.assertTrue(self.task.expected_output(self.task.workload("acceptance", 0)))

    def test_every_trial_went_through_the_backend(self):
        self.assertTrue(self.backend.calls)
        self.assertTrue(all(call.trial_id.startswith("prep-") for call in self.backend.calls))

    def test_correct_faster_candidate_passes_the_screen(self):
        output = tempfile.TemporaryDirectory()
        self.addCleanup(output.cleanup)
        harness = Harness(LocalSubprocessBackend(), output=output.name, seed=self.session["seed"],
                          workload_lines=self.session["workload_lines"], hidden_cases=20,
                          task=self.task)
        source = COUNTER_CANDIDATE
        result = harness.evaluate_candidate({
            "schema_version": 1, "request_id": "r1", "session_id": "repo-prop",
            "task_id": self.task.task_id,
            "candidate": {"candidate_id": "c1", "source_utf8": source.decode(),
                          "sha256": digest(source)},
            "mode": "screen", "remaining_wall_seconds": 900,
            "verifier": {"approved": True, "evidence_ids": ["fixture-verdict"]}})
        self.assertEqual(result["status"], "screened")
        self.assertTrue(result["correctness_passed"])
        wrong = b"def solve(data):\n    return [['zzz', 1]]\n"
        result = harness.evaluate_candidate({
            "schema_version": 1, "request_id": "r2", "session_id": "repo-prop",
            "task_id": self.task.task_id,
            "candidate": {"candidate_id": "c2", "source_utf8": wrong.decode(),
                          "sha256": digest(wrong)},
            "mode": "screen", "remaining_wall_seconds": 900,
            "verifier": {"approved": True, "evidence_ids": ["fixture-verdict"]}})
        self.assertEqual(result["status"], "rejected")

    def test_invalid_proposal_is_refused_before_any_trial(self):
        backend = LocalSubprocessBackend()
        with self.assertRaisesRegex(ValueError, "import of os"):
            prepare_repo_session(
                "repo-bad", str(self.repo), backend=backend, root=self.root,
                proposal={**self.proposal, "generator_source":
                          "import os\ndef generate_input(seed, size):\n    return size\n"})
        self.assertEqual(backend.calls, [])
        self.assertFalse((Path(self.root) / "repo-bad.repo").exists())

    def test_without_proposal_a_missing_spec_file_is_still_an_error(self):
        with self.assertRaisesRegex(ValueError, "nofreewins.json"):
            prepare_repo_session("repo-nospec", str(self.repo), backend=LocalSubprocessBackend(),
                                 root=self.root)

    def test_repo_with_its_own_generated_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "r"
            write_repo(repo, dict(BASIC, **{"_nfw_generated/x.py": "X = 1\n"}))
            (repo / "nofreewins.json").unlink()
            with self.assertRaisesRegex(ValueError, "_nfw_generated"):
                prepare_repo_session(
                    "repo-clash", str(repo), backend=LocalSubprocessBackend(),
                    proposal={"target": "pkg/core.py:f", "generator_source": GOOD_GENERATOR,
                              "include": "full"}, root=self.root,
                    repos_root=str(Path(directory) / "repos"))


class CliProposalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def parse(self, *argv):
        return cli.build_parser().parse_args(["prepare-repo", *argv])

    def test_parsing_and_mutual_exclusion(self):
        args = self.parse("--session-id", "s", "--proposal", "p.json", "--include", "full")
        self.assertEqual((args.proposal, args.repo, args.include), (Path("p.json"), None, "full"))
        args = self.parse("--session-id", "s", "--repo", "some/dir")
        self.assertEqual((args.repo, args.proposal), ("some/dir", None))
        for argv in (["--session-id", "s"],
                     ["--session-id", "s", "--repo", "r", "--proposal", "p.json"],
                     ["--repo", "r"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.parse(*argv)

    def write(self, **changes):
        value = {"repo": "https://github.com/o/n", "root": str(EXAMPLE),
                 "origin_info": {"kind": "github", "commit": "a" * 40},
                 "proposal": {"target": "textstats/core.py:top_words",
                              "generator_source": GOOD_GENERATOR}}
        value.update(changes)
        path = self.base / "p.json"
        path.write_text(json.dumps(value))
        return path

    def test_proposal_file_is_loaded_and_root_checked(self):
        repo, root, info, proposal = cli.load_proposal_file(self.write())
        self.assertEqual((repo, root), ("https://github.com/o/n", EXAMPLE.resolve()))
        self.assertEqual(info["commit"], "a" * 40)
        self.assertEqual(proposal["target"], "textstats/core.py:top_words")
        outside = self.base / "elsewhere"
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, "inside the project"):
            cli.load_proposal_file(self.write(root=str(outside)))
        with self.assertRaisesRegex(ValueError, "not a directory"):
            cli.load_proposal_file(self.write(root=str(EXAMPLE / "missing")))
        # private/repos is also allowed (given explicitly here, as the checkout is a temp dir)
        repo, root, _, _ = cli.load_proposal_file(self.write(root=str(outside)),
                                                  repos=self.base, project=EXAMPLE)
        self.assertEqual(root, outside.resolve())
        link = self.base / "link"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            return
        with self.assertRaisesRegex(ValueError, "inside the project"):
            cli.load_proposal_file(self.write(root=str(link)), repos=EXAMPLE, project=EXAMPLE)

    def test_default_repos_dir_is_anchored_at_the_project_not_the_cwd(self):
        project = self.base / "project"
        (project / "private" / "repos" / "clone").mkdir(parents=True)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "private" / "repos").mkdir(parents=True)
        inside = project / "private" / "repos" / "clone"
        cwd = os.getcwd()
        os.chdir(elsewhere)
        try:
            self.assertEqual(
                cli.load_proposal_file(self.write(root=str(inside)), project=project)[1],
                inside.resolve())
            with self.assertRaisesRegex(ValueError, "inside the project"):
                cli.load_proposal_file(
                    self.write(root=str(elsewhere / "private" / "repos")), project=project)
        finally:
            os.chdir(cwd)

    def test_fetch_repo_default_destination_is_anchored_at_the_project(self):
        project = self.base / "project"
        project.mkdir()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()

        def fake_run(argv, **kwargs):
            if "clone" in argv:
                Path(argv[-1]).mkdir(parents=True)
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(argv, 0, b"a" * 40 + b"\n", b"")

        cwd = os.getcwd()
        os.chdir(elsewhere)
        try:
            with mock.patch.object(subprocess, "run", fake_run), \
                    mock.patch.object(repo_task, "PROJECT_ROOT", project):
                path, _ = fetch_repo("https://github.com/o/n")
        finally:
            os.chdir(cwd)
        self.assertEqual(path.parent, (project / "private" / "repos").resolve())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_proposal_file_shape_errors(self):
        for changes in ({"proposal": []}, {"origin_info": "x"}, {"root": 5}, {"extra": 1},
                        {"repo": 5}):
            with self.assertRaises(ValueError, msg=str(changes)):
                cli.load_proposal_file(self.write(**changes))
        path = self.base / "q.json"
        path.write_text("[1]")
        with self.assertRaises(ValueError):
            cli.load_proposal_file(path)
        path.write_text('{"root": "x", "root": "y"}')
        with self.assertRaises(ValueError):
            cli.load_proposal_file(path)
        with self.assertRaises(ValueError):
            cli.load_proposal_file(self.base / "absent.json")
        path.write_text(json.dumps({"root": str(EXAMPLE), "proposal": {}}))
        _, _, info, _ = cli.load_proposal_file(path)  # origin_info defaults to a local record
        self.assertEqual(info, {"kind": "local", "path": str(EXAMPLE.resolve())})

    def test_main_passes_the_proposal_and_origin_info_through(self):
        calls = []
        path = self.write()

        class Backend:
            def __enter__(self):
                return "backend"

            def __exit__(self, *exc):
                return False

        def fake_prepare(session_id, repo, **kwargs):
            calls.append((session_id, repo, kwargs))
            return {"session_id": session_id}

        argv = ["optimizer_core", "prepare-repo", "--session-id", "s1", "--proposal", str(path)]
        cwd = os.getcwd()
        os.chdir(self.base)  # the commitment file lands in ./artifacts/commitments
        try:
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch("optimizer_core.backend.verified_backend", Backend), \
                    mock.patch.object(repo_task, "prepare_repo_session", fake_prepare), \
                    contextlib.redirect_stdout(io.StringIO()):
                cli.main()
        finally:
            os.chdir(cwd)
        session_id, repo, kwargs = calls[0]
        self.assertEqual((session_id, repo), ("s1", str(EXAMPLE.resolve())))
        self.assertEqual(kwargs["backend"], "backend")
        self.assertEqual(kwargs["origin_info"]["kind"], "github")
        self.assertEqual(kwargs["proposal"]["target"], "textstats/core.py:top_words")
        self.assertIsNone(kwargs["include"])
        self.assertTrue((self.base / "artifacts" / "commitments" / "s1.json").is_file())


class LogsPathUnchangedTests(unittest.TestCase):
    def test_default_task_is_the_logs_task(self):
        from optimizer_core.harness import LogsTask
        from optimizer_core.tasks import BASELINE_SOURCE

        class Stub:
            origin = "synthetic"
            environment_sha256 = digest(b"stub")

        harness = Harness(Stub(), seed="s", workload_lines=20, hidden_cases=3)
        self.assertIsInstance(harness.task, LogsTask)
        self.assertEqual(harness.task.task_id, "logs-parse-v1")
        self.assertEqual(harness.task.baseline_source, BASELINE_SOURCE)
        self.assertEqual(harness.task.executed_source(b"x"), b"x")
        self.assertEqual(harness.task.timing_case("warmup", 0, "s", 5, 7)["id"], "warmup-7")
        self.assertEqual(harness.task.timing_case("screen", 2, "s", 5, 7)["id"], "screen-2")
        self.assertEqual([name for name, _ in harness.task.artifacts()], ["task_definition.py"])


if __name__ == "__main__":
    unittest.main()
