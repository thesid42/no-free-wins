"""A/B integration seam. There is deliberately no local subprocess fallback."""

from dataclasses import dataclass
from typing import Protocol
from pathlib import Path
import base64
import math
import shlex
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace

from .canonical import canonical_json, digest, strict_json
from .config import DaytonaSettings
from .ledger import append_event, verify_ledger


@dataclass(frozen=True)
class TrialRequest:
    trial_id: str
    candidate_source: bytes
    input_bytes: bytes
    wall_timeout_seconds: float
    environment_sha256: str
    outer_timeout_seconds: float = 180


@dataclass(frozen=True)
class TrialResult:
    trial_id: str
    stdout: bytes
    exit_code: int
    trusted_process_seconds: float
    environment_sha256: str
    sandbox_id: str
    measurement_proof_sha256: str
    cleanup_confirmed: bool
    proof_bytes: bytes = b""
    origin: str = "measured"
    failure_reason: str = ""
    end_to_end_seconds: float = 0


class SandboxBackend(Protocol):
    def run_trial(self, request: TrialRequest) -> TrialResult:
        """Only a probe-verified remote implementation may supply real timings."""
        ...


class InfrastructureError(RuntimeError):
    pass


PROBE_SOURCE = '''
def solve(data):
    import os, socket, time
    results = {"uid": os.getuid()}
    def denied(name, action):
        try:
            action()
            results[name] = 0
        except PermissionError:
            results[name] = 1
    private = os.path.dirname(os.path.dirname(__file__)) + "/private/proof.json"
    denied("protected_result", lambda: open(private, "w"))
    denied("host_file", lambda: open("/etc/shadow", "rb"))
    denied("global_write", lambda: open("/tmp/optimizer-global-marker", "w"))
    denied("signal_supervisor", lambda: os.kill(os.getppid(), 0))
    denied("network", lambda: socket.socket())
    denied("fork", lambda: os.fork())
    results["no_keys"] = int(not any("KEY" in name or "TOKEN" in name for name in os.environ))
    time.perf_counter = lambda: 0
    time.monotonic = lambda: 0
    time.sleep(0.025)
    with open("allowed-marker", "w") as stream:
        stream.write("scratch")
    results["scratch_write"] = 1
    return results
'''


class DaytonaBackend:
    """Real SDK adapter. Unknown isolation, proof or cleanup fails closed."""

    origin = "measured"

    def __init__(self, settings=None, *, client=None, params_factory=None,
                 registry="artifacts/daytona_registry.jsonl"):
        self.settings = settings or DaytonaSettings.load()
        self.settings.validate(require_key=client is None)
        if client is None:
            try:
                from daytona import Daytona, DaytonaConfig, CreateSandboxFromSnapshotParams
            except ImportError as exc:
                raise InfrastructureError("Install the pinned Daytona dependency first") from exc
            client = Daytona(DaytonaConfig(
                api_key=self.settings.api_key, api_url=self.settings.api_url,
                target=self.settings.target or None, otel_enabled=False))
            params_factory = CreateSandboxFromSnapshotParams
        if params_factory is None:
            from types import SimpleNamespace
            params_factory = SimpleNamespace
        self.client, self.params_factory = client, params_factory
        self.registry = Path(registry)
        self.lock = threading.RLock()
        self.verified = False
        self.environment_sha256 = ""
        self.count = 0
        self.used_seconds = 0.0
        self.runner_bytes = Path(__file__).with_name("remote_runner.py").read_bytes()
        self.driver_bytes = Path(__file__).with_name("remote_driver.py").read_bytes()
        self.probe_report = None
        self.sandbox = None
        self.sandbox_name = None
        self.reservation = 0
        self.opened_at = None
        self.closed = False

    def _event(self, **event):
        append_event(self.registry, {"type": "sandbox_lifecycle", **event})

    def _confirmed_delete(self, sandbox):
        try:
            self.client.delete(sandbox, timeout=30, wait=True)
        except Exception as exc:
            if type(exc).__name__ != "DaytonaNotFoundError":
                return False
        try:
            remaining = self.client.get(sandbox.id, request_timeout=10)
            state = getattr(remaining.state, "value", remaining.state)
            return state == "destroyed"
        except Exception as exc:
            return type(exc).__name__ == "DaytonaNotFoundError"

    def _reserve(self):
        reserve = self.settings.ttl_minutes * 60
        if self.count >= self.settings.max_sandboxes:
            raise InfrastructureError("sandbox count budget exhausted")
        if self.used_seconds + reserve > self.settings.max_compute_seconds:
            raise InfrastructureError("sandbox wall-time reservation exhausted")
        if ((self.used_seconds + reserve) / 3600 *
                self.settings.conservative_hourly_usd > self.settings.run_budget_usd):
            raise InfrastructureError("sandbox estimated-cost reservation exhausted")
        self.count += 1
        self.used_seconds += reserve
        return reserve

    def _open(self):
        if self.closed:
            raise InfrastructureError("execution session already closed")
        if self.sandbox is not None:
            return self.sandbox
        self.reservation = self._reserve()
        self.opened_at = time.monotonic()
        self.sandbox_name = "optimizer-" + uuid.uuid4().hex
        self._event(name=self.sandbox_name, action="creating", ttl_minutes=self.settings.ttl_minutes)
        params = dict(name=self.sandbox_name, language="python", os_user="root",
                      public=False, ephemeral=True, network_block_all=True,
                      auto_stop_interval=2, auto_delete_interval=0,
                      ttl_minutes=self.settings.ttl_minutes, env_vars={}, secrets={},
                      volumes=[], labels={"project": "recursive-optimizer"})
        if self.settings.snapshot:
            params["snapshot"] = self.settings.snapshot
        if self.settings.target:
            params["target"] = self.settings.target
        self.sandbox = self.client.create(self.params_factory(**params), timeout=60)
        self._event(name=self.sandbox_name, sandbox_id=self.sandbox.id, action="created")
        return self.sandbox

    def _close_event(self, **event):
        """Registry write for close(): a ledger hiccup (e.g. a concurrent writer mid-line) is
        retried briefly and never allowed to abort cleanup."""
        for attempt in range(3):
            try:
                self._event(**event)
                return
            except Exception:
                time.sleep(0.2 * (attempt + 1))

    def close(self):
        if self.closed:
            return self.sandbox is None
        self.verified = False  # no more trials; an interrupted close can simply be called again
        cleaned = self.sandbox_name is None
        if self.sandbox is None and self.sandbox_name:
            try:
                self.sandbox = self.client.get(self.sandbox_name, request_timeout=10)
            except Exception:
                pass
        if self.sandbox is not None:
            cleaned = self._confirmed_delete(self.sandbox)
            if not cleaned:  # one retry: a transient SDK error must not leak a sandbox
                cleaned = self._confirmed_delete(self.sandbox)
        if self.sandbox_name:
            self._close_event(name=self.sandbox_name,
                              sandbox_id=getattr(self.sandbox, "id", None),
                              action="deleted" if cleaned else "cleanup_unconfirmed")
        if cleaned:
            if self.opened_at is not None:
                self.used_seconds -= max(0, self.reservation - (time.monotonic()-self.opened_at))
            self.sandbox = None
        self.closed = True
        return cleaned

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        if not self.close() and exception[0] is None:
            raise InfrastructureError("sandbox deletion could not be confirmed")

    def _execute(self, request, *, discovery=False):
        if not 0.05 <= request.wall_timeout_seconds <= 15:
            raise ValueError("invalid remote process deadline")
        if len(request.candidate_source)>262144 or len(request.input_bytes)>16<<20:
            raise ValueError("candidate/input size limit")
        if not discovery and request.environment_sha256 != self.environment_sha256:
            raise InfrastructureError("unrecognized frozen environment")
        start = time.monotonic()
        deadline = start + min(180, request.outer_timeout_seconds)
        def timeout(cap):
            remaining=deadline-time.monotonic()-40
            if remaining<1:
                raise InfrastructureError("remote operation deadline exhausted")
            return max(1,min(cap,int(remaining)))
        phase="create"
        root="/tmp/optimizer-"+uuid.uuid4().hex
        try:
            sandbox=self._open()
            if time.monotonic()-self.opened_at > self.settings.ttl_minutes*60-30:
                raise InfrastructureError("sandbox lifetime nearly exhausted")
            metadata={key:getattr(sandbox,key,None) for key in ("cpu","memory","disk")}
            if getattr(sandbox, "gpu_type", None):
                raise InfrastructureError("GPU snapshots are outside the CPU budget/contract")
            if any(type(value) not in (int,float) or value<=0 for value in metadata.values()):
                raise InfrastructureError("provider resource metadata missing")
            if metadata["cpu"]>4 or metadata["memory"]>8 or metadata["disk"]>10:
                raise InfrastructureError("snapshot exceeds reserved resource envelope")
            phase="prepare"
            setup=("import os; from pathlib import Path; "
                   f"p=Path({root!r}); p.mkdir(mode=0o700); "
                   "[(p/n).mkdir(mode=0o700) for n in ('private','public','scratch')]; "
                   "print(os.geteuid())")
            result=sandbox.process.exec("python3 -I -c "+shlex.quote(setup),timeout=timeout(15))
            if result.exit_code != 0:
                raise InfrastructureError("remote workspace setup failed")
            api_uid=int(result.result.strip())
            python="python3 -I " if api_uid==0 else "sudo -n -- python3 -I "
            uploads={"/private/runner.py":self.runner_bytes,
                     "/private/request.json":canonical_json({"trial_id":request.trial_id,
                                      "timeout_seconds":request.wall_timeout_seconds}),
                     "/private/input.json":request.input_bytes,
                     "/public/driver.py":self.driver_bytes,
                     "/public/solution.py":request.candidate_source}
            for path,data in uploads.items():
                phase="upload:"+path
                sandbox.fs.upload_file(data,root+path,timeout=timeout(20))
            phase="supervise"
            result=sandbox.process.exec(python+shlex.quote(root+"/private/runner.py")+" "+
                                       shlex.quote(root),timeout=timeout(math.ceil(request.wall_timeout_seconds)+15))
            if result.exit_code != 0:
                (self.registry.parent/"runner_diagnostic.json").write_bytes(canonical_json(
                    {"exit_code":result.exit_code,"stdout":getattr(result,"result","")[:6000]}))
                raise InfrastructureError("Linux security/supervisor setup failed")
            phase="download"
            proof_bytes=sandbox.fs.download_file(root+"/private/proof.json",timeout(20))
            if len(proof_bytes)>4<<20:
                raise InfrastructureError("oversized measurement proof")
            proof=strict_json(proof_bytes)
            expected={"trial_id":request.trial_id,"source_sha256":digest(request.candidate_source),
                      "input_sha256":digest(request.input_bytes),"runner_sha256":digest(self.runner_bytes),
                      "driver_sha256":digest(self.driver_bytes)}
            if any(proof.get(key)!=value for key,value in expected.items()):
                raise InfrastructureError("measurement proof is not bound to this trial")
            security=proof.get("security",{})
            if (security.get("candidate_uid")!=65534 or type(security.get("landlock_abi")) is not int
                    or security["landlock_abi"]<3 or security.get("seccomp") is not True
                    or security.get("single_process") is not True):
                raise InfrastructureError("isolation proof missing")
            seconds=proof.get("duration_seconds")
            if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<=0:
                raise InfrastructureError("invalid trusted timer result")
            if seconds>request.wall_timeout_seconds+3 or type(proof.get("exit_code")) is not int:
                raise InfrastructureError("invalid process termination evidence")
            environment=digest(canonical_json({"runtime":proof["environment"],"resources":metadata,
                         "snapshot":self.settings.snapshot or "provider-default",
                         "runner":digest(self.runner_bytes),"driver":digest(self.driver_bytes)}))
            if not discovery and environment != request.environment_sha256:
                raise InfrastructureError("runtime/resource fingerprint changed")
            stdout=base64.b64decode(proof["stdout_b64"],validate=True)
            if len(stdout)>1<<20:
                raise InfrastructureError("oversized output")
            # All candidate writes are confined to scratch, all IPC/fork are denied.
            # Remove the whole trial tree after its process has been reaped.
            phase="trial-cleanup"
            cleanup=("import shutil; from pathlib import Path; "
                     f"p=Path({root!r}); assert p.parent==Path('/tmp') and "
                     "p.name.startswith('optimizer-') and not p.is_symlink(); shutil.rmtree(p)")
            result=sandbox.process.exec(python+"-c "+shlex.quote(cleanup),timeout=timeout(15))
            if result.exit_code != 0:
                raise InfrastructureError("trial state cleanup failed")
            return TrialResult(request.trial_id,stdout,proof["exit_code"],seconds,environment,
                       sandbox.id,digest(proof_bytes),True,proof_bytes=proof_bytes,
                       failure_reason=proof.get("reason",""),end_to_end_seconds=time.monotonic()-start)
        except InfrastructureError:
            self.close()
            raise
        except Exception as exc:
            import re
            message=str(exc)
            if self.settings.api_key:
                message=message.replace(self.settings.api_key,"[redacted]")
            message=re.sub(r"https?://\S+","[url]",message)
            self.registry.parent.mkdir(parents=True,exist_ok=True)
            (self.registry.parent/"sdk_diagnostic.json").write_bytes(canonical_json(
                {"phase":phase,"error_type":type(exc).__name__,"message":message[:2000]}))
            self.close()
            raise InfrastructureError("Daytona "+phase+" failed: "+type(exc).__name__) from None

    def probe(self):
        from .canonical import parse_output
        import os
        with self.lock:
            self.verified = False
            payload = canonical_json({"cases": [{"id": "probe", "input": {}}]})
            if os.environ.get("NFW_DEMO_SKIP_PROBES") == "1":
                # Demo speed mode: one plain discovery trial pins the environment hash. The
                # supervisor still applies Landlock/seccomp/limits on every trial and fails closed;
                # only the adversarial probe battery is skipped, and receipts record that.
                first = self._execute(TrialRequest("environment-discovery",
                                                   b"def solve(data):\n    return 1\n",
                                                   payload, 2.0, ""), discovery=True)
                if first.exit_code or first.failure_reason:
                    raise InfrastructureError("environment discovery failed")
                self.environment_sha256 = first.environment_sha256
                self.isolation_probe = "skipped (NFW_DEMO_SKIP_PROBES=1)"
                self.verified = True
                self.probe_report = {"verified": False, "isolation_probe": self.isolation_probe,
                                     "environment_sha256": self.environment_sha256}
                return self.probe_report
            self.isolation_probe = "passed"
            first = self._execute(TrialRequest("isolation-probe", PROBE_SOURCE.encode(),
                                              payload, 2.0, ""), discovery=True)
            response = strict_json(parse_output(first.stdout))
            result = response["results"][0]["output"]
            expected = {"uid": 65534, "protected_result": 1, "host_file": 1,
                        "global_write": 1, "signal_supervisor": 1, "network": 1,
                        "fork": 1, "no_keys": 1, "scratch_write": 1}
            if (first.exit_code or first.failure_reason or result != expected or
                    first.trusted_process_seconds < 0.025):
                raise InfrastructureError("isolation/timer probe failed")
            self.environment_sha256 = first.environment_sha256
            hang = self._execute(TrialRequest(
                "timeout-probe", b"def solve(data):\n    while True: pass\n",
                payload, 0.15, self.environment_sha256))
            if hang.failure_reason != "process_timeout" or hang.exit_code == 0:
                raise InfrastructureError("process deadline probe failed")
            probe_dir = self.registry.parent / "probes" / uuid.uuid4().hex
            probe_dir.mkdir(parents=True)
            (probe_dir / "isolation.json").write_bytes(first.proof_bytes)
            (probe_dir / "timeout.json").write_bytes(hang.proof_bytes)
            self.verified = True
            self.probe_report = {"verified": True, "environment_sha256": self.environment_sha256,
                                 "proofs": [first.measurement_proof_sha256,
                                            hang.measurement_proof_sha256],
                                 "cleanup_confirmed": True,
                                 "proof_directory": str(probe_dir)}
            return self.probe_report

    def run_trial(self, request: TrialRequest) -> TrialResult:
        with self.lock:
            if not self.verified:
                raise InfrastructureError("run Daytona isolation probes before candidate execution")
            return self._execute(request)

    def reconcile(self):
        """Only names recorded by this controller, never unrelated sandboxes."""
        latest = {}
        if self.registry.exists():
            for record in verify_ledger(self.registry):
                event = record["event"]
                latest[event["name"]] = event
        results = []
        for name, event in latest.items():
            if event["action"] == "deleted":
                continue
            cleaned = False
            try:
                sandbox = self.client.get(event.get("sandbox_id") or name, request_timeout=10)
                cleaned = self._confirmed_delete(sandbox)
            except Exception as exc:
                cleaned = type(exc).__name__ == "DaytonaNotFoundError"
            self._event(name=name, action="deleted" if cleaned else "cleanup_unconfirmed")
            results.append({"name": name, "deleted": cleaned})
        return results


@contextmanager
def verified_backend(settings=None):
    """At most three clean allocations to find a runtime supporting all controls.

    Only preflight can retry allocations. Candidate measurements never rerun
    automatically to search for a favorable score.
    """
    settings = settings or DaytonaSettings.load()
    spent_seconds = 0
    last_error = None
    for attempt in range(min(3, settings.max_sandboxes)):
        remaining = settings.max_compute_seconds - spent_seconds
        remaining_usd = (settings.run_budget_usd -
                         spent_seconds / 3600 * settings.conservative_hourly_usd)
        if (remaining < settings.ttl_minutes * 60 or
                remaining_usd < settings.ttl_minutes * 60 / 3600 *
                settings.conservative_hourly_usd):
            break
        current = replace(settings, max_compute_seconds=remaining,
                          max_sandboxes=settings.max_sandboxes-attempt,
                          run_budget_usd=remaining_usd)
        backend = DaytonaBackend(current)
        try:
            backend.probe()
        except InfrastructureError as exc:
            last_error = exc
            if not backend.close():
                raise InfrastructureError("preflight failed and deletion is unconfirmed") from None
            spent_seconds += backend.used_seconds
            if backend.sandbox_name:
                backend._event(name=backend.sandbox_name, action="deleted",
                               recovery="preflight_failed", attempt=attempt+1)
            continue
        try:
            yield backend
        except BaseException:
            # Any failure (ValueError, timeout, interrupt) still deletes the sandbox, and a
            # cleanup problem must never replace the error the caller needs to see.
            try:
                backend.close()
            except Exception:
                pass
            raise
        if not backend.close():
            raise InfrastructureError("evaluation sandbox deletion unconfirmed")
        return
    raise InfrastructureError("no verified runtime after bounded preflight attempts") from last_error
