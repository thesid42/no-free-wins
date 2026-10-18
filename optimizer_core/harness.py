"""A's high-level JSON interface. Candidate source is never executed locally."""

import ast
from dataclasses import asdict
import math
import os
from pathlib import Path
import secrets
from statistics import median
import time
import uuid
import threading
from contextlib import contextmanager

from .acceptance import TimingPair, evaluate
from .backend import InfrastructureError, TrialRequest
from .canonical import canonical_json, digest, parse_output, strict_json
from .ledger import append_event
from .receipts import audit_receipt, create_receipt
from .tasks import BASELINE_SOURCE, correctness_cases, lines_for, reference


class EvaluationDeadline(TimeoutError):
    pass


class CandidateFailure(ValueError):
    pass


class LogsTask:
    """The frozen logs-parse-v1 task, exactly as it behaved before tasks were pluggable."""
    task_id = "logs-parse-v1"
    baseline_source = BASELINE_SOURCE

    def correctness_cases(self, seed, count, full_size):
        return correctness_cases(seed, count=count, full_size=full_size)

    def expected_output(self, case):
        return reference(case["input"])

    def timing_case(self, phase, index, seed, size, counter):
        label = f"{phase}-{counter if phase == 'warmup' else index}"
        return {"id": label, "input": lines_for(seed, label, size)}

    def executed_source(self, source):
        return source

    def artifacts(self):
        return [("task_definition.py", Path(__file__).with_name("tasks.py").read_bytes())]


def checked_trial(backend, origin, trial_id, source, payload, timeout, outer_timeout):
    """One backend trial plus the identity, cleanup and proof checks every trial needs."""
    request = TrialRequest(trial_id, source, payload, timeout,
                           backend.environment_sha256, outer_timeout_seconds=outer_timeout)
    result = backend.run_trial(request)
    if result.trial_id != request.trial_id or result.origin != origin:
        raise InfrastructureError("backend result identity/origin mismatch")
    if (not result.cleanup_confirmed or
            result.environment_sha256 != request.environment_sha256 or
            not result.proof_bytes or
            digest(result.proof_bytes) != result.measurement_proof_sha256):
        raise InfrastructureError("missing or inconsistent execution/cleanup proof")
    return result


def correctness_batches(cases):
    """The exact correctness batch plan: up to 256 cases and 1 MiB of input per trial."""
    batch, batch_bytes = [], 0
    for case in cases:
        size = len(canonical_json(case))
        if batch and (len(batch) >= 256 or batch_bytes + size > 1 << 20):
            yield batch
            batch, batch_bytes = [], 0
        batch.append(case)
        batch_bytes += size
    if batch:
        yield batch


def parse_results(stdout, ids):
    """Strict {"results": [{"id", "output"}]} envelope; every expected id exactly once."""
    wire = strict_json(parse_output(stdout))
    if set(wire) != {"results"} or type(wire["results"]) is not list:
        raise ValueError("wrong result envelope")
    expected, found = set(ids), {}
    for row in wire["results"]:
        if (type(row) is not dict or set(row) != {"id", "output"} or
                type(row["id"]) is not str or row["id"] not in expected or
                row["id"] in found):
            raise ValueError("missing/duplicate/unknown result")
        found[row["id"]] = row["output"]
    if set(found) != expected:
        raise ValueError("incomplete results")
    return found


def policy_findings(source):
    """Narrow evidence, not a complete intent classifier."""
    tree = ast.parse(source.decode("utf-8"))
    time_names = {"time"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            time_names.update(alias.asname or "time" for alias in node.names
                              if alias.name == "time")
    findings = []
    clocks = {"time", "perf_counter", "monotonic", "perf_counter_ns", "monotonic_ns"}
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
        for target in targets:
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id in time_names and target.attr in clocks):
                findings.append("clock_patch")
    return sorted(set(findings))


class Harness:
    def __init__(self, backend, *, output="artifacts/evaluations", seed=None,
                 workload_lines=100_000, hidden_cases=2000, task=None):
        self.backend = backend
        self.task = task or LogsTask()
        self.output = Path(output)
        self.seed = seed or secrets.token_hex(16)
        self.workload_lines, self.hidden_cases = workload_lines, hidden_cases
        self.origin = getattr(backend, "origin", None)
        self.lock = threading.RLock()
        if self.origin not in ("synthetic", "measured"):
            raise ValueError("backend must declare its trusted measurement origin")
        if self.origin == "measured" and hidden_cases != 2000:
            raise ValueError("measured correctness requires all 2000 hidden cases")
        if not 1 <= workload_lines <= 100_000:
            raise ValueError("workload exceeds frozen input bounds")

    def _trial(self, source, cases, *, timeout=2):
        if time.monotonic() + 60 > self.deadline:
            raise EvaluationDeadline("insufficient time for bounded remote cleanup")
        payload = canonical_json({"cases": cases})
        trial_id = f"trial-{self.trial_count:04d}"
        self.trial_count += 1
        result = checked_trial(self.backend, self.origin, trial_id, source, payload, timeout,
                               min(180, self.deadline - time.monotonic()))
        proof_path = self.directory / f"proof-{self.trial_count:04d}.json"
        proof_path.write_bytes(result.proof_bytes)
        self.artifacts.append({"path": proof_path.name,
                               "sha256": result.measurement_proof_sha256})
        if time.monotonic() > self.deadline:
            raise EvaluationDeadline("evaluation deadline reached")
        if result.exit_code != 0 or result.failure_reason:
            raise CandidateFailure("candidate_process_failed")
        try:
            expected = {case["id"]: case for case in cases}
            found = parse_results(result.stdout, expected)
            for identifier, case in expected.items():
                if canonical_json(found[identifier]) != canonical_json(
                        self.task.expected_output(case)):
                    raise ValueError("output mismatch")
        except (ValueError, TypeError, KeyError) as exc:
            raise CandidateFailure("output_or_schema_mismatch") from exc
        return result, digest(parse_output(result.stdout))

    def _correctness(self, source):
        for batch in correctness_batches(self.task.correctness_cases(
                self.seed, self.hidden_cases, self.workload_lines)):
            self._trial(source, batch, timeout=15)

    def _pairs(self, source, phase, count):
        pairs = []
        for index in range(count):
            case = self.task.timing_case(phase, index, self.seed, self.workload_lines,
                                         self.trial_count)
            order = ("baseline", "candidate") if (
                int(digest((self.seed + case["id"]).encode()), 16) % 2) else (
                "candidate", "baseline")
            results = {}
            for name in order:
                try:
                    results[name] = self._trial(
                        self.task.baseline_source if name == "baseline" else source, [case])
                except CandidateFailure as exc:
                    if name == "baseline":
                        raise InfrastructureError("frozen baseline failed") from exc
                    raise
            baseline, before_hash = results["baseline"]
            candidate, after_hash = results["candidate"]
            for duration in (baseline.trusted_process_seconds, candidate.trusted_process_seconds):
                if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
                    raise InfrastructureError("invalid process duration")
            pairs.append(TimingPair(
                digest(canonical_json({"cases": [case]})), baseline.trusted_process_seconds,
                candidate.trusted_process_seconds, before_hash, after_hash,
                candidate.environment_sha256))
        return pairs

    @contextmanager
    def _stage(self, seconds):
        previous = self.deadline
        self.deadline = min(previous, time.monotonic() + seconds)
        try:
            yield
        finally:
            self.deadline = previous

    def evaluate_candidate(self, request):
        with self.lock:
            return self._evaluate_candidate(request)

    def _evaluate_candidate(self, request):
        allowed = {"schema_version", "request_id", "session_id", "task_id",
                   "candidate", "mode", "remaining_wall_seconds", "verifier"}
        if (type(request) is not dict or set(request) != allowed or
                type(request["schema_version"]) is not int or request["schema_version"] != 1 or
                request["task_id"] != self.task.task_id or
                request["mode"] not in ("screen", "acceptance")):
            raise ValueError("unsupported candidate-evaluation request")
        for key in ("request_id", "session_id"):
            if type(request[key]) is not str or not 1 <= len(request[key]) <= 128:
                raise ValueError("invalid request/session identifier")
        candidate = request["candidate"]
        if type(candidate) is not dict or set(candidate) != {"candidate_id", "source_utf8", "sha256"}:
            raise ValueError("invalid candidate envelope")
        if (type(candidate["candidate_id"]) is not str or
                not 1 <= len(candidate["candidate_id"]) <= 128 or
                type(candidate["source_utf8"]) is not str):
            raise ValueError("invalid candidate identity/source")
        source = candidate["source_utf8"].encode("utf-8")
        if len(source) > 262_144 or digest(source) != candidate["sha256"]:
            raise ValueError("candidate source hash/size mismatch")
        remaining = request["remaining_wall_seconds"]
        if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining <= 0:
            raise ValueError("invalid remaining wall budget")
        if self.origin == "measured" and not getattr(self.backend, "verified", False):
            raise InfrastructureError("Daytona must pass isolation probes first")
        verifier = request["verifier"]
        approved = (type(verifier) is dict and verifier.get("approved") is True
                    and type(verifier.get("evidence_ids")) is list
                    and bool(verifier["evidence_ids"])
                    and all(type(item) is str and item for item in verifier["evidence_ids"]))
        self.deadline = time.monotonic() + min(1800, remaining)
        self.trial_count, self.artifacts = 0, []
        self.directory = self.output / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        for filename, data in (
            ("candidate.py", source), ("baseline.py", self.task.baseline_source),
            ("request.json", canonical_json(request)),
            ("private_inputs.json", canonical_json(
                {"seed": self.seed, "workload_lines": self.workload_lines,
                 "hidden_cases": self.hidden_cases})),
            *self.task.artifacts(),
            ("remote_runner.py", Path(__file__).with_name("remote_runner.py").read_bytes()),
            ("remote_driver.py", Path(__file__).with_name("remote_driver.py").read_bytes()),
            ("acceptance.py", Path(__file__).with_name("acceptance.py").read_bytes()),
        ):
            (self.directory / filename).write_bytes(data)
            self.artifacts.append({"path": filename, "sha256": digest(data)})
        import platform
        from importlib.metadata import PackageNotFoundError, version
        try:
            sdk_version = version("daytona")
        except PackageNotFoundError:
            sdk_version = "not-installed-synthetic"
        runtime = canonical_json({"controller_python": platform.python_version(),
                                  "daytona_sdk": sdk_version,
                                  "environment_sha256": self.backend.environment_sha256,
                                  "isolation_probe": getattr(self.backend, "isolation_probe", "passed")})
        (self.directory / "runtime.json").write_bytes(runtime)
        self.artifacts.append({"path": "runtime.json", "sha256": digest(runtime)})
        response = {"schema_version": 1, "request_id": request["request_id"],
                    "candidate_id": candidate["candidate_id"], "status": "rejected",
                    "origin": self.origin, "accepted": False, "correctness_passed": False,
                    "screen_median_speedup": None, "acceptance": None,
                    "receipt_id": None, "receipt_sha256": None,
                    "evidence_ids": [], "feedback_public": []}
        try:
            try:
                findings = policy_findings(source)
            except (SyntaxError, UnicodeError):
                raise CandidateFailure("invalid_python_source") from None
            if findings:
                raise CandidateFailure("policy_" + findings[0])
            executed = self.task.executed_source(source)
            if executed != source:
                (self.directory / "executed_candidate.py").write_bytes(executed)
                self.artifacts.append({"path": "executed_candidate.py",
                                       "sha256": digest(executed)})
            if request["mode"] == "acceptance" and not approved:
                raise CandidateFailure("verifier_evidence_required")
            with self._stage(180):
                self._correctness(executed)
            response["correctness_passed"] = True
            if request["mode"] == "screen":
                with self._stage(300):
                    pairs = self._pairs(executed, "screen", 5)
                response["screen_median_speedup"] = median(
                    pair.baseline_seconds / pair.candidate_seconds for pair in pairs)
                response["status"] = "screened"
            else:
                with self._stage(600):
                    for index in range(5):
                        # Warmups use distinct data; no warmup result enters the CI.
                        case = self.task.timing_case("warmup", index, self.seed,
                                                     self.workload_lines, self.trial_count)
                        self._trial(self.task.baseline_source, [case])
                        self._trial(executed, [case])
                    pairs = self._pairs(executed, "acceptance", 30)
                # No accepted receipt while its evaluation machine is still allocated.
                if self.origin == "measured":
                    if not self.backend.close():
                        raise InfrastructureError("evaluation sandbox cleanup unconfirmed")
                    cleanup = canonical_json({"sandbox_deleted": True,
                                              "environment_sha256": self.backend.environment_sha256})
                    (self.directory / "cleanup.json").write_bytes(cleanup)
                    self.artifacts.append({"path": "cleanup.json", "sha256": digest(cleanup)})
                gates = {"correctness_passed": True, "verifier_passed": approved,
                         "evidence_complete": self.origin == "measured"}
                receipt = create_receipt(pairs, origin=self.origin, gates=gates,
                                         artifacts=list(self.artifacts))
                receipt_path = self.directory / "receipt.json"
                receipt_path.write_bytes(canonical_json(receipt))
                anchor = digest(receipt_path.read_bytes())
                decision = audit_receipt(receipt_path, expected_sha256=anchor)
                response.update(
                    status=("accepted" if decision["accepted"] else
                            "simulated_acceptance" if self.origin == "synthetic" else "bar_not_met"),
                    accepted=decision["accepted"], acceptance=decision,
                    receipt_id=self.directory.name, receipt_sha256=anchor)
            response["evidence_ids"] = [self.directory.name]
        except EvaluationDeadline:
            response["status"] = "budget_exhausted"
            response["feedback_public"] = [{"code": "evaluation_deadline",
                                           "summary": "Evaluation ended at its wall budget",
                                           "evidence_ids": [self.directory.name]}]
        except CandidateFailure as exc:
            response["feedback_public"] = [{"code": str(exc),
                                           "summary": "Candidate failed a required gate",
                                           "evidence_ids": [self.directory.name]}]
        except InfrastructureError:
            response["status"] = "infrastructure_error"
            response["feedback_public"] = [{"code": "execution_or_cleanup_failure",
                                           "summary": "Trusted execution evidence is unavailable",
                                           "evidence_ids": [self.directory.name]}]
        finally:
            if self.origin == "measured" and not self.backend.close():
                response["status"] = "infrastructure_error"
                response["accepted"] = False
                response["feedback_public"] = [{"code": "cleanup_unconfirmed",
                                               "summary": "Remote deletion is not confirmed",
                                               "evidence_ids": [self.directory.name]}]
        (self.directory / "response.json").write_bytes(canonical_json(response))
        append_event(self.output / "ledger.jsonl",
                     {"type": "acceptance" if response["accepted"] else "evaluation",
                      "session": request["session_id"], "origin": self.origin,
                      "response_sha256": digest(canonical_json(response)),
                      "receipt_sha256": response["receipt_sha256"],
                      "accepted": response["accepted"]})
        return response


def evaluate_candidate(request):
    """B's drop-in API; one disposable machine per evaluation, serial timing."""
    if type(request) is not dict:
        raise ValueError("evaluation request must be a JSON object")
    with _eval_slots():
        from .backend import verified_backend
        from .sessions import load_session
        session = load_session(request.get("session_id", ""))
        if "repo" in session:
            from .repo_task import RepoTask
            task = RepoTask.load(session)
        else:
            task = LogsTask()
        with verified_backend() as backend:
            return Harness(backend, seed=session["seed"], workload_lines=session["workload_lines"],
                           task=task).evaluate_candidate(request)


# Evaluations are independent: each owns its Harness, its sandbox and its output directory, and
# timing pairs are matched within one machine. A bounded semaphore (default 2, NFW_MAX_PARALLEL_EVALS
# 1..4) replaces the old global lock; with one caller the behavior is identical to serial.
MAX_PARALLEL_EVALS_ENV = "NFW_MAX_PARALLEL_EVALS"
DEFAULT_PARALLEL_EVALS = 2
_slots, _slots_guard = None, threading.Lock()


def max_parallel_evals(environ=None):
    raw = (os.environ if environ is None else environ).get(MAX_PARALLEL_EVALS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PARALLEL_EVALS
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if not 1 <= value <= 4:
        raise ValueError(f"{MAX_PARALLEL_EVALS_ENV} must be an integer from 1 to 4")
    return value


def _eval_slots():
    global _slots
    with _slots_guard:
        if _slots is None:
            _slots = threading.BoundedSemaphore(max_parallel_evals())
        return _slots
