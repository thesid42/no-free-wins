"""Contract-faithful SDK fake: does not execute Python source or call Daytona."""

import base64
import re
import shlex
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from optimizer_core.backend import (DaytonaBackend, InfrastructureError, PROBE_SOURCE,
                                    TrialRequest, verified_backend)
from optimizer_core.canonical import canonical_json, digest, strict_json
from optimizer_core.config import DaytonaSettings


class DaytonaNotFoundError(Exception):
    pass


class FakeFiles:
    def __init__(self):
        self.files = {}
    def upload_file(self, src, dst, timeout=1800):
        self.files[dst] = src
    def download_file(self, path, *args):
        assert len(args) == 1 and type(args[0]) is int  # SDK overload takes positional timeout.
        return self.files[path]


class FakeProcess:
    def __init__(self, client, fs):
        self.client, self.fs = client, fs
        self.commands = []
    def exec(self, command, timeout=None):
        self.commands.append(command)
        if "print(os.geteuid())" in command:
            return SimpleNamespace(exit_code=0, result="1000\n")
        if "shutil.rmtree" in command:
            code = shlex.split(command)[-1]
            root = re.search(r"p=Path\('([^']+)'\)", code).group(1)
            self.fs.files = {key: value for key, value in self.fs.files.items()
                             if not key.startswith(root + "/")}
            return SimpleNamespace(exit_code=0, result="")
        root = shlex.split(command)[-1]
        files = self.fs.files
        source = files[root + "/public/solution.py"]
        req = strict_json(files[root + "/private/request.json"])
        if source == PROBE_SOURCE.encode():
            output = {"uid": 65534, "protected_result": 1, "host_file": 1,
                      "global_write": 1, "signal_supervisor": 1, "network": 1,
                      "fork": 1, "no_keys": 1, "scratch_write": 1}
            wire = {"results": [{"id": "probe", "output": output}]}
            reason, exit_code, duration = "", 0, 0.04
        else:
            wire = {}
            reason, exit_code, duration = "process_timeout", -9, 0.16
        proof = {"schema_version": 1, "trial_id": req["trial_id"],
                 "source_sha256": digest(source),
                 "input_sha256": digest(files[root + "/private/input.json"]),
                 "runner_sha256": digest(files[root + "/private/runner.py"]),
                 "driver_sha256": digest(files[root + "/public/driver.py"]),
                 "environment": {"machine": "fake-linux", "cpu_max": "100000 100000"},
                 "duration_seconds": duration, "exit_code": exit_code, "reason": reason,
                 "stdout_b64": base64.b64encode(canonical_json(wire)).decode(),
                 "stderr_b64": "",
                 "security": {"candidate_uid": 65534, "landlock_abi": 3,
                              "seccomp": True, "single_process": True}}
        if self.client.corrupt:
            proof["source_sha256"] = "0" * 64
        files[root + "/private/proof.json"] = canonical_json(proof)
        return SimpleNamespace(exit_code=0, result="")


class FakeClient:
    def __init__(self, *, corrupt=False, delete_fails=False):
        self.corrupt, self.delete_fails = corrupt, delete_fails
        self.creates, self.deleted = 0, False
        self.fs = FakeFiles()
        self.process = FakeProcess(self, self.fs)
    def create(self, params, timeout=60):
        self.creates += 1
        assert params.network_block_all and params.ephemeral and not params.public
        assert params.env_vars == {} and params.secrets == {} and params.volumes == []
        self.sandbox = SimpleNamespace(id="fake-sandbox", cpu=2, memory=4, disk=3,
                                       state="started", fs=self.fs, process=self.process)
        return self.sandbox
    def delete(self, sandbox, timeout=60, wait=False):
        assert wait is True
        if self.delete_fails:
            raise TimeoutError("injected deletion failure")
        self.deleted = True
    def get(self, name, request_timeout=10):
        if self.deleted or not self.creates:
            raise DaytonaNotFoundError()
        return self.sandbox


class DaytonaAdapterTests(unittest.TestCase):
    def backend(self, directory, client):
        return DaytonaBackend(DaytonaSettings(api_key="unit-secret"), client=client,
                              registry=Path(directory) / "registry.jsonl")

    def test_probes_share_machine_but_clear_each_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            backend = self.backend(directory, client)
            self.assertTrue(backend.probe()["verified"])
            self.assertEqual(client.creates, 1)
            self.assertEqual(client.fs.files, {})
            self.assertTrue(backend.close())
            self.assertTrue(client.deleted)

    def test_forged_proof_rejected_and_machine_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(corrupt=True)
            backend = self.backend(directory, client)
            with self.assertRaises(InfrastructureError):
                backend.probe()
            self.assertTrue(client.deleted)

    def test_missing_probe_blocks_candidate_before_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            backend = self.backend(directory, client)
            with self.assertRaises(InfrastructureError):
                backend.run_trial(TrialRequest("bad", b"source", b"{}", 2, ""))
            self.assertEqual(client.creates, 0)

    def test_unconfirmed_deletion_cannot_close_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(delete_fails=True)
            backend = self.backend(directory, client)
            backend.probe()
            self.assertFalse(backend.close())
            self.assertFalse(client.deleted)

    def test_source_never_interpolated_into_a_shell_command(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            backend = self.backend(directory, client)
            backend.probe()
            self.assertNotIn("time.perf_counter = lambda", "\n".join(client.process.commands))
            backend.close()

    def test_preflight_recovery_respects_combined_budget(self):
        settings_seen = []
        objects = []
        def factory(settings):
            settings_seen.append(settings)
            obj = SimpleNamespace(used_seconds=5, sandbox_name="owned-unit")
            obj.close = lambda: True
            obj._event = lambda **event: None
            if not objects:
                def fail():
                    raise InfrastructureError("unsupported unit-test runtime")
                obj.probe = fail
            else:
                obj.probe = lambda: {"verified": True}
            objects.append(obj)
            return obj
        with patch("optimizer_core.backend.DaytonaBackend", side_effect=factory):
            with verified_backend(DaytonaSettings(api_key="unit")) as backend:
                self.assertIs(backend, objects[1])
        self.assertEqual(len(settings_seen), 2)
        self.assertEqual(settings_seen[1].max_compute_seconds,
                         settings_seen[0].max_compute_seconds - 5)
        self.assertLess(settings_seen[1].run_budget_usd, settings_seen[0].run_budget_usd)

    def test_preflight_never_retries_more_than_three_allocations(self):
        def factory(settings):
            def fail():
                raise InfrastructureError("unsupported unit-test runtime")
            return SimpleNamespace(probe=fail, close=lambda: True, used_seconds=1,
                                   sandbox_name="owned-unit", _event=lambda **event: None)
        with patch("optimizer_core.backend.DaytonaBackend", side_effect=factory) as mocked:
            with self.assertRaises(InfrastructureError):
                with verified_backend(DaytonaSettings(api_key="unit")):
                    pass
            self.assertEqual(mocked.call_count, 3)
