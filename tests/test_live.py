import contextlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from agent_team import live
from agent_team.__main__ import PLAN, SOURCE_A, SOURCE_B, main
from agent_team.contract import make_candidate
from agent_team.models import FixtureModelClient, MalformedOutput
from agent_team.orchestrator import DEFAULT_MIN_HARNESS_SECONDS
from agent_team.roles import parse_candidate, parse_plan, parse_verdict
from optimizer_core import sessions
from optimizer_core.ledger import verify_ledger

ROOT = Path(__file__).resolve().parents[1]
LIVE_IDS = [
    "Qwen/Qwen3.8-27B", "deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4.1-Flash",
    "deepseek-ai/Deepseek-V4-Flash", "google/gemma-4-31b-it",
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B",
    "nvidia/NVIDIA-Nemotron-3.5-Super-VL", "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B",
    "nvidia/Nemotron-3.5-Lightning-30B-A3B", "openai/gpt-oss-120b", "zai-org/GLM-5.3",
    "zai-org/GLM-5.3-Flash"]
SUPER = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
GLM_FLASH = "zai-org/GLM-5.3-Flash"
DEEPSEEK_FLASH = "deepseek-ai/Deepseek-V4-Flash"
SHA_A = make_candidate("worker_a-r1", SOURCE_A)["sha256"]
GOOD = "def solve(lines):\n    return {}\n"


def fence(source):
    return f"```python\n{source}```"


class ListingClient(FixtureModelClient):
    """Fixture inference plus a canned /models listing; never touches the network."""

    def __init__(self, script=None, ids=LIVE_IDS):
        super().__init__(script or {})
        self.ids, self.listed, self.max_tokens = list(ids), 0, []

    def complete(self, model_id, role, messages, max_tokens):
        self.max_tokens.append(max_tokens)
        return super().complete(model_id, role, messages, max_tokens)

    def list_models(self):
        self.listed += 1
        return sorted(self.ids)


def run_script(**overrides):
    verdict = json.dumps({"approved": True, "candidate_sha256": SHA_A, "rationale": "honest"})
    base = {SUPER: [PLAN], NANO: [fence(SOURCE_A), fence(SOURCE_B)], GLM_FLASH: [verdict]}
    base.update(overrides)
    return base


def measured_response(request, **changes):
    mode = request["mode"]
    response = {"schema_version": 1, "request_id": request["request_id"],
                "candidate_id": request["candidate"]["candidate_id"], "status": "screened",
                "origin": "measured", "accepted": False, "correctness_passed": True,
                "screen_median_speedup": 4.0, "acceptance": None, "receipt_id": None,
                "receipt_sha256": None, "evidence_ids": ["ev-" + mode], "feedback_public": []}
    if mode == "acceptance":
        response.update(
            status="accepted", accepted=True, screen_median_speedup=None,
            acceptance={"accepted": True, "origin": "measured", "pairs": 30, "median_speedup": 3.5,
                        "ci_lower": 3.4, "reasons": []},
            receipt_id="receipt-1", receipt_sha256="a" * 64)
    response.update(changes)
    return response


class FakeEvaluate:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        self.calls.append(request["mode"])
        if request["mode"] == "screen":
            speedup = 4.0 if request["candidate"]["candidate_id"] == "worker_a-r1" else 2.0
            return measured_response(request, screen_median_speedup=speedup)
        return measured_response(request)


def no_preflight(session_id):
    return lambda: None


def logs_session(session_id, **kw):
    """A prepared logs session (no repo block), so tests that skip the preflight never read private/."""
    return {"schema_version": 1, "session_id": session_id}


class LineupTests(unittest.TestCase):
    def test_resolves_against_the_live_listing(self):
        orders = live.resolve_lineup(LIVE_IDS)
        self.assertEqual((orders["planner"].primary, orders["planner"].fallback), (SUPER, NANO))
        for role in ("worker_a", "worker_b"):
            self.assertEqual((orders[role].primary, orders[role].fallback), (SUPER, NANO))
        self.assertEqual((orders["verifier"].primary, orders["verifier"].fallback),
                         (GLM_FLASH, DEEPSEEK_FLASH))

    def test_missing_or_ambiguous_name_names_the_role(self):
        with self.assertRaisesRegex(ValueError, "verifier"):
            live.resolve_lineup([i for i in LIVE_IDS if "Flash" not in i or "V4.1" in i])
        with self.assertRaisesRegex(ValueError, "planner.*ambiguous"):
            live.resolve_lineup(LIVE_IDS + ["other/nemotron-3-super-x"])
        with self.assertRaisesRegex(ValueError, "worker_a"):
            live.resolve_lineup(LIVE_IDS, {"worker_a": ("no-such-model", "gpt-oss-120b")})


class BudgetTests(unittest.TestCase):
    def test_priced_with_all_model_prices(self):
        orders = live.resolve_lineup(LIVE_IDS)
        budget = live.live_budget(orders, max_cost_usd=5)
        snapshot = budget.snapshot()
        self.assertTrue(snapshot["priced"])
        self.assertEqual(set(budget.prices), {SUPER, NANO, GLM_FLASH, DEEPSEEK_FLASH})
        self.assertEqual((snapshot["max_attempts"], snapshot["max_tokens"], snapshot["task_attempts"],
                          snapshot["max_cost_usd"]), (60, 180_000, 12, 5))
        self.assertEqual(live.live_budget(orders, max_cost_usd=25).max_cost_usd, 25)

    def test_cost_bounds(self):
        orders = live.resolve_lineup(LIVE_IDS)
        for bad in (0, -1, 26, 25.01, math.nan, math.inf, "5", True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                live.live_budget(orders, max_cost_usd=bad)


class TimedListingClient(ListingClient):
    """ListingClient that is timeout-aware: records plain and timed calls separately."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.plain, self.timed = [], []

    def complete(self, model_id, role, messages, max_tokens):
        self.plain.append((model_id, role))
        return super().complete(model_id, role, messages, max_tokens)

    def complete_with_timeout(self, model_id, role, messages, max_tokens, timeout_seconds):
        self.timed.append((model_id, role, timeout_seconds))
        return ListingClient.complete(self, model_id, role, messages, max_tokens)


class BuildLiveRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "run"
        for patcher in (mock.patch.object(live, "make_preflight", no_preflight),
                        mock.patch.object(sessions, "load_session", logs_session)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, client=None, **kw):
        client = client or ListingClient(run_script())
        kw.setdefault("evaluate", FakeEvaluate())
        kw.setdefault("session_id", "dev-session")
        kw.setdefault("parallel", False)  # these fixtures share one model queue between workers
        return live.build_live_run(output_dir=self.output, client=client,
                                   available_ids=LIVE_IDS, **kw), client

    def test_orchestrator_defaults_and_info(self):
        (orchestrator, info), _ = self.build(max_tokens=2048)
        # Live floors are the real-harness defaults plus the provisioning margin: only ever raised.
        self.assertEqual(orchestrator.min_harness_seconds, {"screen": 360, "acceptance": 960})
        for mode, floor in DEFAULT_MIN_HARNESS_SECONDS.items():
            self.assertEqual(orchestrator.min_harness_seconds[mode],
                             floor + live.HARNESS_SETUP_MARGIN_SECONDS)
        self.assertEqual(orchestrator.max_tokens, 2048)
        self.assertTrue(orchestrator.budget.snapshot()["priced"])
        self.assertTrue(self.output.is_dir())
        self.assertEqual(Path(orchestrator.ledger_path), self.output / "ledger.jsonl")
        self.assertEqual(info["models"]["verifier"], {"primary": GLM_FLASH, "fallback": DEEPSEEK_FLASH})
        self.assertEqual(info["deadline_seconds"], 1800)
        self.assertEqual(info["harness_floors"], {"screen": 360, "acceptance": 960})
        self.assertIn(SUPER, info["prices"])
        self.assertEqual(info["budget"]["max_cost_usd"], 5.0)

    def test_lists_models_when_not_injected(self):
        client = ListingClient(run_script())
        live.build_live_run(session_id="dev-session", output_dir=self.output, client=client,
                            evaluate=FakeEvaluate())
        self.assertEqual(client.listed, 1)

    def test_full_fake_run_reaches_verified_done_and_labels_the_ledger(self):
        evaluate = FakeEvaluate()
        (orchestrator, _), client = self.build(evaluate=evaluate)
        result = orchestrator.run()
        self.assertEqual(result.state, "verified_done")
        self.assertEqual(evaluate.calls, ["screen", "screen", "acceptance"])
        records = verify_ledger(result.ledger_path)
        events = result.events
        self.assertEqual([e["type"] for e in events][:3], ["run_started", "run_label", "preflight"])
        label = events[1]
        self.assertEqual(label["label"], "development live run; not an official session")
        self.assertEqual((label["harness"], label["models"]), ("optimizer_core real", "crusoe live"))
        self.assertEqual(records[-1]["sha256"], result.ledger_final_sha256)
        self.assertEqual(len(records), len(events))
        self.assertEqual({call[0] for call in client.calls}, {SUPER, NANO, GLM_FLASH})

    def test_drill_injects_two_worker_a_failures_and_the_fallback_recovers(self):
        evaluate = FakeEvaluate()
        (orchestrator, info), client = self.build(evaluate=evaluate, inject_failures=True)
        self.assertEqual(info["drill"], {"role": "worker_a", "model_id": SUPER, "failures": 2})
        result = orchestrator.run()
        self.assertEqual(result.state, "verified_done")
        self.assertEqual(evaluate.calls, ["screen", "screen", "acceptance"])
        types_ = [e["type"] for e in result.events]
        self.assertEqual(types_[:4], ["run_started", "fault_injection", "run_label", "preflight"])
        drill, label = result.events[1], result.events[2]
        self.assertEqual((drill["model_id"], drill["failures"]), (SUPER, 2))
        self.assertIn("labeled drill", drill["label"])
        self.assertEqual(label["label"],
                         "development live run; not an official session; labeled fault drill")
        created = [e for e in result.events if e["type"] == "candidate_created"
                   and e["worker"] == "worker_a"][0]
        attempts = created["attempts"]
        self.assertEqual([a["outcome"] for a in attempts], ["model_error", "model_error", "ok"])
        self.assertEqual([a["model_id"] for a in attempts], [SUPER, SUPER, NANO])
        for attempt in attempts[:2]:
            self.assertTrue(attempt["detail"].startswith("injected fault: "))
        self.assertEqual(orchestrator.client.injected_total, 2)
        self.assertEqual(client.max_tokens.count(8192), len(client.max_tokens))  # inner client reached
        self.assertEqual(len(client.calls), len(client.max_tokens))
        planner = [e for e in result.events if e["type"] == "plan"][0]
        self.assertEqual(len(planner["attempts"]), 1)  # the shared primary model was not drilled
        verify_ledger(result.ledger_path)

    def test_drill_on_a_timeout_aware_client_routes_every_call_through_complete_with_timeout(self):
        client = TimedListingClient(run_script())
        (orchestrator, _), _ = self.build(client=client, inject_failures=True)
        result = orchestrator.run()
        self.assertEqual(result.state, "verified_done")
        self.assertEqual(client.plain, [])  # nothing bypassed the timeout-aware path
        self.assertEqual({role for _, role, _ in client.timed},
                         {"planner", "worker_a", "worker_b", "verifier"})
        for _, _, timeout in client.timed:
            self.assertTrue(math.isfinite(timeout) and timeout > 0, timeout)
        # worker_a's two primary calls were injected and never reached the inner client.
        self.assertEqual([m for m, role, _ in client.timed if role == "worker_a"], [NANO])
        self.assertEqual(orchestrator.client.injected_total, 2)
        created = [e for e in result.events if e["type"] == "candidate_created"
                   and e["worker"] == "worker_a"][0]
        self.assertEqual([a["outcome"] for a in created["attempts"]], ["model_error", "model_error", "ok"])

    def test_shim_exposes_complete_with_timeout_only_when_the_inner_client_has_it(self):
        def shim(inner):
            return live._RoleScopedFaults(
                inner, live.FaultInjectingClient(inner, faults={SUPER: 1}), "worker_a")
        self.assertFalse(hasattr(shim(ListingClient(run_script())), "complete_with_timeout"))
        self.assertTrue(hasattr(shim(TimedListingClient(run_script())), "complete_with_timeout"))

    def test_without_the_flag_nothing_changes(self):
        (orchestrator, info), client = self.build()
        self.assertNotIn("drill", info)
        self.assertIs(orchestrator.client, client)
        result = orchestrator.run()
        self.assertEqual([e["type"] for e in result.events][:3],
                         ["run_started", "run_label", "preflight"])
        self.assertNotIn("fault_injection", [e["type"] for e in result.events])
        self.assertEqual(result.events[1]["label"], "development live run; not an official session")

    def test_max_tokens_reaches_every_model_call(self):
        for requested in (8192, 2048):
            self.output = Path(self.tmp.name) / f"run-{requested}"
            (orchestrator, _), client = self.build(max_tokens=requested)
            self.assertEqual(orchestrator.run().state, "verified_done")
            self.assertTrue(client.max_tokens)
            self.assertEqual(set(client.max_tokens), {requested})

    def test_default_max_tokens_is_8192_on_the_live_path(self):
        client = ListingClient(run_script())
        orchestrator, _ = live.build_live_run(
            session_id="dev-session", output_dir=self.output, client=client,
            evaluate=FakeEvaluate(), available_ids=LIVE_IDS, parallel=False)
        orchestrator.run()
        self.assertEqual(set(client.max_tokens), {8192})

    def test_raised_floors_end_the_run_when_time_is_short(self):
        # 900 s left clears the old 840 s acceptance floor but not the live 960 s floor.
        (orchestrator, _), _ = self.build(deadline_seconds=900)
        result = orchestrator.run()
        self.assertEqual(result.state, "failed_deadline")
        self.assertIn("insufficient time for acceptance", result.events[-1]["reason"])

    def test_bad_session_id_and_max_tokens_fail_before_the_directory_exists(self):
        for bad in ("../../escaped", "a/b", "/tmp/x", "", "x" * 129, "a b"):
            with self.assertRaises(ValueError, msg=bad):
                self.build(session_id=bad)
        for bad in (255, 32769, 8192.0, True):  # None means the config default
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.build(max_tokens=bad)
        self.assertFalse(self.output.exists())

    def test_non_empty_output_dir_is_rejected(self):
        self.output.mkdir()
        (self.output / "old.txt").write_text("x")
        with self.assertRaisesRegex(ValueError, "not empty"):
            self.build()
        self.output.joinpath("old.txt").unlink()
        self.build()  # empty existing directory is fine

    def test_bad_deadline_fails_before_the_directory_exists(self):
        for deadline in (0, -5, 1801, math.nan, math.inf):
            with self.assertRaises(ValueError, msg=str(deadline)):
                self.build(deadline_seconds=deadline)
            self.assertFalse(self.output.exists())

    def test_bad_cost_fails_before_the_directory_exists(self):
        with self.assertRaises(ValueError):
            self.build(max_cost_usd=26)
        self.assertFalse(self.output.exists())

    def test_unresolvable_lineup_fails_before_the_directory_exists(self):
        with self.assertRaisesRegex(ValueError, "planner"):
            live.build_live_run(session_id="s", output_dir=self.output, client=ListingClient(),
                                evaluate=FakeEvaluate(), available_ids=["only/one-model"])
        self.assertFalse(self.output.exists())


class PreflightTests(unittest.TestCase):
    def test_unprepared_session_leaves_no_run_dir_and_no_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_load = sessions.load_session

            def load(session_id):
                return real_load(session_id, root=Path(tmp) / "sessions")  # never the real private/
            client = ListingClient(run_script())
            with mock.patch.object(sessions, "load_session", load):
                with self.assertRaisesRegex(ValueError, "Prepare this session"):
                    live.build_live_run(session_id="not-prepared", output_dir=Path(tmp) / "run",
                                        client=client, evaluate=FakeEvaluate(),
                                        available_ids=LIVE_IDS)
            self.assertFalse((Path(tmp) / "run").exists())
            self.assertEqual((client.calls, client.listed), ([], 0))

    def test_orchestrator_preflight_failure_ends_failed_infrastructure_with_no_model_calls(self):
        outcomes = iter([None, ValueError("session vanished")])

        def flaky(session_id):
            def preflight():
                outcome = next(outcomes)
                if outcome:
                    raise outcome
            return preflight
        with tempfile.TemporaryDirectory() as tmp:
            client, evaluate = ListingClient(run_script()), FakeEvaluate()
            with mock.patch.object(live, "make_preflight", flaky), \
                    mock.patch.object(sessions, "load_session", logs_session):
                orchestrator, _ = live.build_live_run(
                    session_id="s1", output_dir=Path(tmp) / "run", client=client,
                    evaluate=evaluate, available_ids=LIVE_IDS, parallel=False)
                result = orchestrator.run()
        self.assertEqual(result.state, "failed_infrastructure")
        self.assertEqual((client.calls, evaluate.calls), ([], []))
        self.assertEqual([e["type"] for e in result.events],
                         ["run_started", "run_label", "preflight", "terminal"])
        self.assertIs(result.events[2]["ok"], False)

    def test_preflight_checks_session_daytona_settings_and_sdk(self):
        settings = mock.Mock()
        with mock.patch.object(sessions, "load_session", return_value={}) as load, \
                mock.patch.object(live.config.DaytonaSettings, "load", return_value=settings), \
                mock.patch.object(importlib.metadata, "version", return_value="0.225.0") as version:
            live.make_preflight("dev-session")()
        load.assert_called_once_with("dev-session")
        settings.validate.assert_called_once_with()
        version.assert_called_once_with("daytona")

    def test_preflight_fails_without_a_daytona_key(self):
        with mock.patch.object(sessions, "load_session", return_value={}), \
                mock.patch.object(live.config.DaytonaSettings, "load",
                                  return_value=live.config.DaytonaSettings(api_key="")):
            with self.assertRaisesRegex(ValueError, "DAYTONA_API_KEY"):
                live.make_preflight("dev-session")()

    def test_preflight_fails_without_the_sdk(self):
        with mock.patch.object(sessions, "load_session", return_value={}), \
                mock.patch.object(live.config.DaytonaSettings, "load", return_value=mock.Mock()), \
                mock.patch.object(importlib.metadata, "version",
                                  side_effect=importlib.metadata.PackageNotFoundError("daytona")):
            with self.assertRaisesRegex(RuntimeError, "daytona"):
                live.make_preflight("dev-session")()


class RealEvaluateMarginTests(unittest.TestCase):
    def test_harness_gets_less_time_on_a_copy_of_the_request(self):
        seen = []

        def harness(request):
            seen.append(request)
            return "response"
        fake_module = types.ModuleType("optimizer_core.harness")
        fake_module.evaluate_candidate = harness
        request = {"remaining_wall_seconds": 1000.0, "mode": "screen", "candidate": {"a": 1}}
        original = json.loads(json.dumps(request))
        with mock.patch.dict(sys.modules, {"optimizer_core.harness": fake_module}):
            self.assertEqual(live.real_evaluate(request), "response")
            self.assertEqual(seen[0]["remaining_wall_seconds"], 1000.0 - 120)
            self.assertEqual({k: v for k, v in seen[0].items() if k != "remaining_wall_seconds"},
                             {k: v for k, v in original.items() if k != "remaining_wall_seconds"})
            self.assertEqual(request, original)  # the caller's (logged) request is unchanged
            self.assertIsNot(seen[0], request)
            live.real_evaluate({**request, "remaining_wall_seconds": 100.0})
            live.real_evaluate({**request, "remaining_wall_seconds": 121.5})
        self.assertEqual([r["remaining_wall_seconds"] for r in seen[1:]], [1.0, 1.5])


class ImportIsolationTests(unittest.TestCase):
    def test_importing_live_loads_no_harness_backend_repo_task_or_daytona(self):
        code = ("import sys, agent_team.live, agent_team.analyst, agent_team.__main__\n"
                "bad = [m for m in ('optimizer_core.harness', 'optimizer_core.backend',\n"
                "                   'optimizer_core.repo_task', 'daytona')\n"
                "       if m in sys.modules]\n"
                "print(','.join(bad))")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "")


REPO_TASK_ID = "repo-abc123def456"
REPO_SUMMARY = "REPO-SUMMARY-MARKER: optimize solve(data) in pkg/mod.py"
SEED = "feedfacefeedfacefeedfacefeedface"


def repo_session(session_id, **kw):
    return {"schema_version": 1, "session_id": session_id, "seed": SEED,
            "repo": {"task_id": REPO_TASK_ID, "spec_sha256": "1" * 64, "bundle_sha256": "2" * 64,
                     "origin_info": {"kind": "git", "commit": "c" * 300, "remote": ["not", "scalar"]},
                     "include": ["pkg/mod.py"], "n_files": 3, "bundle_bytes": 4096}}


def fake_repo_task_module(loaded):
    module = types.ModuleType("optimizer_core.repo_task")

    class RepoTask:
        task_id = REPO_TASK_ID

        @classmethod
        def load(cls, session, root="private/sessions"):
            loaded.append(session)
            return cls()

        def public_summary(self):
            return REPO_SUMMARY
    module.RepoTask = RepoTask
    return module


class RepoSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "run"
        self.loaded = []
        for patcher in (mock.patch.object(live, "make_preflight", no_preflight),
                        mock.patch.object(sessions, "load_session", repo_session),
                        mock.patch.dict(sys.modules,
                                        {"optimizer_core.repo_task": fake_repo_task_module(self.loaded)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, **kw):
        client = ListingClient(run_script())
        return live.build_live_run(session_id="dev-session", output_dir=self.output, client=client,
                                   evaluate=kw.pop("evaluate", FakeEvaluate()),
                                   available_ids=LIVE_IDS, parallel=False, **kw), client

    def test_build_uses_repo_task_id_summary_and_label(self):
        (orchestrator, info), _ = self.build()
        self.assertEqual(orchestrator.task_id, REPO_TASK_ID)
        self.assertEqual(orchestrator.task_summary, REPO_SUMMARY)
        self.assertEqual(len(self.loaded), 1)
        label = orchestrator.annotations[0]
        self.assertEqual(label, {
            "type": "run_label", "label": "development live run on user repo; not an official session",
            "task_id": REPO_TASK_ID, "repo_kind": "git", "repo_commit": "c" * 200,
            "bundle_sha256": "2" * 64})
        self.assertEqual(info["task"], {"task_id": REPO_TASK_ID, "kind": "repo",
                                        "include": ["pkg/mod.py"], "n_files": 3, "bundle_bytes": 4096})
        self.assertNotIn(SEED, json.dumps(info) + json.dumps(label))

    def test_repo_run_sends_task_id_and_summary_and_labels_the_ledger(self):
        requests = []

        def evaluate(request):
            requests.append(request)
            return FakeEvaluate()(request)
        (orchestrator, _), client = self.build(evaluate=evaluate)
        result = orchestrator.run()
        self.assertEqual(result.state, "verified_done")
        self.assertTrue(requests)
        self.assertEqual({r["task_id"] for r in requests}, {REPO_TASK_ID})
        for _, _, messages in client.calls:
            self.assertIn(REPO_SUMMARY, json.dumps(messages))
            self.assertNotIn("logs-parse-v1", json.dumps(messages))
        events = [r["event"] for r in verify_ledger(orchestrator.ledger_path)]
        self.assertEqual(events[0]["task_id"], REPO_TASK_ID)
        self.assertEqual(events[1]["type"], "run_label")
        self.assertEqual(events[1]["repo_kind"], "git")

    def test_check_live_reports_the_repo_task(self):
        info = live.check_live("dev-session", available_ids=LIVE_IDS)
        self.assertEqual(info["task"], {"task_id": REPO_TASK_ID, "kind": "repo",
                                        "include": ["pkg/mod.py"], "n_files": 3, "bundle_bytes": 4096})
        self.assertEqual(len(self.loaded), 1)

    def test_logs_session_is_unchanged_and_never_imports_repo_task(self):
        self.loaded.clear()
        with mock.patch.object(sessions, "load_session", logs_session), \
                mock.patch.dict(sys.modules, {"optimizer_core.repo_task": None}):  # import would raise
            (orchestrator, info), _ = self.build()
            checked = live.check_live("dev-session", available_ids=LIVE_IDS)
        self.assertEqual(orchestrator.task_id, "logs-parse-v1")
        self.assertEqual(orchestrator.task_summary, live.roles.TASK_SUMMARY)
        self.assertEqual(orchestrator.annotations, [live.RUN_LABEL])
        self.assertEqual(info["task"]["kind"], "logs")
        self.assertEqual(checked["task"], {"task_id": "logs-parse-v1", "kind": "logs", "include": None,
                                           "n_files": None, "bundle_bytes": None})
        self.assertEqual(self.loaded, [])

    def test_check_cli_prints_the_task(self):
        out = io.StringIO()
        with mock.patch.object(live.CrusoeClient, "from_env", return_value=ListingClient(run_script())), \
                contextlib.redirect_stdout(out):
            code = main(["run", "--session-id", "s1", "--check"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["task"]["task_id"], REPO_TASK_ID)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "run"
        self.client = ListingClient(run_script())

    def invoke(self, *argv, preflight=None, evaluate=None):
        out, err = io.StringIO(), io.StringIO()
        patches = [mock.patch.object(live.CrusoeClient, "from_env", return_value=self.client),
                   mock.patch.object(live, "make_preflight", preflight or no_preflight),
                   mock.patch.object(sessions, "load_session", logs_session)]
        if evaluate is not None:
            patches.append(mock.patch.object(live, "real_evaluate", evaluate))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_check_ok_makes_no_calls_and_no_directory(self):
        code, out, _ = self.invoke("run", "--session-id", "s1", "--output", str(self.output), "--check")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["check"], "ok")
        self.assertEqual(report["models"]["planner"], {"primary": SUPER, "fallback": NANO})
        self.assertEqual(report["deadline_seconds"], 1800)
        self.assertEqual(report["harness_floors"], {"screen": 360, "acceptance": 960})
        self.assertTrue(report["budget"]["priced"])
        self.assertIn(GLM_FLASH, report["prices"])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.listed, 1)

    def test_check_preflight_failure_exits_1(self):
        def failing(session_id):
            def preflight():
                raise ValueError("Prepare this session before starting agents")
            return preflight
        code, out, err = self.invoke("run", "--session-id", "s1", "--output", str(self.output),
                                     "--check", preflight=failing)
        self.assertEqual((code, out), (1, ""))
        report = json.loads(err)
        self.assertEqual(report["check"], "failed")
        self.assertIn("Prepare this session", report["error"])
        self.assertFalse(self.output.exists())
        self.assertEqual(self.client.listed, 0)

    def test_traversal_session_ids_are_usage_errors_with_nothing_created(self):
        cwd = os.getcwd()
        work = Path(self.tmp.name) / "cwd"
        work.mkdir()
        os.chdir(work)
        self.addCleanup(os.chdir, cwd)
        for session in ("../../escaped", "a/b", "/tmp/x", "a b", "x" * 129):
            for extra in ([], ["--check"]):
                code, out, err = self.invoke("run", "--session-id", session, *extra)
                self.assertEqual((code, out), (2, ""), (session, extra))
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn("Traceback", err)
        self.assertEqual(list(work.iterdir()), [])
        self.assertEqual((self.client.listed, self.client.calls), (0, []))

    def test_unprepared_session_in_a_full_run_exits_1_without_a_run_dir(self):
        def unprepared(session_id):
            def preflight():
                raise ValueError("Prepare this session before starting agents")
            return preflight
        code, out, err = self.invoke("run", "--session-id", "s1", "--output", str(self.output),
                                     preflight=unprepared, evaluate=FakeEvaluate())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Prepare this session", json.loads(err)["error"])
        self.assertFalse(self.output.exists())
        self.assertEqual((self.client.listed, self.client.calls), (0, []))

    def test_corrupt_session_file_is_a_one_line_error_not_a_traceback(self):
        def corrupt(session_id):
            def preflight():
                raise KeyError("seed")
            return preflight
        for extra in ([], ["--check"]):
            code, out, err = self.invoke("run", "--session-id", "s1", "--output", str(self.output),
                                         *extra, preflight=corrupt)
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(len(err.strip().splitlines()), 1)
            self.assertIn("KeyError", json.loads(err)["error"])
            self.assertFalse(self.output.exists())

    def test_max_tokens_range_is_a_usage_error(self):
        for value in ("0", "255", "32769", "-1"):
            code, out, err = self.invoke("run", "--session-id", "s1", "--max-tokens", value)
            self.assertEqual((code, out), (2, ""), value)
            self.assertEqual(len(err.strip().splitlines()), 1)
        for value in ("256", "32768"):
            code, _, _ = self.invoke("run", "--session-id", "s1", "--check", "--max-tokens", value)
            self.assertEqual(code, 0, value)

    def test_check_unresolvable_lineup_exits_1(self):
        self.client.ids = ["only/one-model"]
        code, _, err = self.invoke("run", "--session-id", "s1", "--check")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["check"], "failed")

    def test_usage_errors_exit_2_with_one_line(self):
        self.output.mkdir()
        (self.output / "old.txt").write_text("x")
        for argv in (["--deadline", "0"], ["--deadline", "1801"], ["--deadline", "nan"],
                     ["--max-cost-usd", "0"], ["--max-cost-usd", "26"], ["--max-rounds", "0"]):
            code, out, err = self.invoke("run", "--session-id", "s1", "--output",
                                         str(Path(self.tmp.name) / "other"), *argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertEqual(len(err.strip().splitlines()), 1, argv)
            self.assertNotIn("Traceback", err)
        code, _, err = self.invoke("run", "--session-id", "s1", "--output", str(self.output))
        self.assertEqual(code, 2)
        self.assertIn("not empty", err)
        self.assertEqual(self.client.calls, [])

    def test_full_run_exit_0_only_on_verified_done(self):
        code, out, _ = self.invoke("run", "--session-id", "s1", "--output", str(self.output),
                                   evaluate=FakeEvaluate())
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual((summary["state"], summary["ledger_verified"]), ("verified_done", True))
        self.assertEqual(summary["last_forwarded_candidate_id"], "worker_a-r1")
        self.assertEqual(summary["models"]["worker_a"]["primary"], SUPER)
        self.assertIn("development live run", summary["note"])
        for key in ("run_id", "rounds", "ledger_path", "ledger_final_sha256", "budget"):
            self.assertIn(key, summary)

    def test_inject_failures_run_reports_the_drill(self):
        code, out, _ = self.invoke("run", "--session-id", "s1", "--output", str(self.output),
                                   "--inject-failures", evaluate=FakeEvaluate())
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["state"], "verified_done")
        self.assertEqual(summary["drill"], {"role": "worker_a", "model_id": SUPER, "failures": 2,
                                            "injected_total": 2})

    def test_plain_run_summary_has_no_drill(self):
        code, out, _ = self.invoke("run", "--session-id", "s1", "--output", str(self.output),
                                   evaluate=FakeEvaluate())
        self.assertEqual(code, 0)
        self.assertNotIn("drill", json.loads(out))

    def test_check_shows_the_drill_plan_and_makes_no_calls(self):
        code, out, _ = self.invoke("run", "--session-id", "s1", "--output", str(self.output),
                                   "--check", "--inject-failures")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["drill"], {"role": "worker_a", "model_id": SUPER, "failures": 2})
        self.assertEqual(self.client.calls, [])
        self.assertFalse(self.output.exists())
        code, out, _ = self.invoke("run", "--session-id", "s1", "--output", str(self.output), "--check")
        self.assertNotIn("drill", json.loads(out))

    def test_full_run_not_verified_exits_1(self):
        def unmeasured(request):
            return measured_response(request, screen_median_speedup=1.0) \
                if request["mode"] == "screen" else measured_response(request)
        self.client = ListingClient(run_script(**{SUPER: [PLAN], NANO: [fence(SOURCE_A)] * 9}))
        code, out, _ = self.invoke("run", "--session-id", "s1", "--output", str(self.output),
                                   "--max-rounds", "1", evaluate=unmeasured)
        self.assertEqual(code, 1)
        self.assertNotEqual(json.loads(out)["state"], "verified_done")

    def test_default_output_is_under_artifacts_runs(self):
        seen = {}

        def capture(**kw):
            seen.update(kw)
            raise ValueError("stop here")
        with mock.patch.object(live, "build_live_run", capture):
            code, _, _ = self.invoke("run", "--session-id", "s1")
        self.assertEqual(code, 1)
        parts = Path(seen["output_dir"]).parts
        self.assertEqual(parts[:2], ("artifacts", "runs"))
        self.assertRegex(parts[2], r"^s1-\d{8}T\d{6}Z$")

    def test_simulate_still_completes(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["simulate", "--output", str(self.output)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["state"], "simulation_completed")


class TolerantRoleTests(unittest.TestCase):
    SHA = "b" * 64
    PLAN_JSON = json.dumps({"strategies": [{"strategy_id": "a", "description": "one pass"},
                                           {"strategy_id": "b", "description": "dict"}]})
    VERDICT = {"approved": True, "candidate_sha256": SHA, "rationale": "ok"}

    def test_fenced_json_plan(self):
        for fence_tag in ("```json\n", "```\n"):
            plan = parse_plan(f"Here is the plan:\n{fence_tag}{self.PLAN_JSON}\n```\nHope it helps.")
            self.assertEqual([s["strategy_id"] for s in plan], ["a", "b"])

    def test_think_block_then_json_verdict(self):
        text = f'<think>maybe {{"approved": false}}</think>\n{json.dumps(self.VERDICT)}'
        self.assertTrue(parse_verdict(text, self.SHA)["approved"])

    def test_stray_closing_think_and_unterminated_think(self):
        self.assertTrue(parse_verdict(f"reasoning</think>{json.dumps(self.VERDICT)}", self.SHA)["approved"])
        with self.assertRaises(MalformedOutput):  # reasoning never finished: JSON inside is not an answer
            parse_verdict(f"<think>{json.dumps(self.VERDICT)}", self.SHA)

    def test_extra_keys_are_ignored_and_dropped(self):
        verdict = parse_verdict(json.dumps({**self.VERDICT, "confidence": 0.9}), self.SHA)
        self.assertEqual(set(verdict), {"approved", "candidate_sha256"})
        plan_text = json.dumps({"strategies": json.loads(self.PLAN_JSON)["strategies"], "notes": "x"})
        self.assertEqual(len(parse_plan(plan_text)), 2)
        # rationale is optional now; the two decision fields are not
        self.assertTrue(parse_verdict(json.dumps({"approved": True, "candidate_sha256": self.SHA}),
                                      self.SHA)["approved"])

    def test_verdict_wrapped_in_prose(self):
        text = f"Sure! My verdict is {json.dumps(self.VERDICT)} -- let me know if you need more."
        self.assertTrue(parse_verdict(text, self.SHA)["approved"])

    def test_sha_binding_and_types_stay_strict(self):
        wrapped = f"```json\n{json.dumps(self.VERDICT)}\n```"
        mismatch = parse_verdict(wrapped, "c" * 64)
        self.assertEqual((mismatch["approved"], mismatch["mismatch"]), (False, True))
        for bad in ({"approved": "true", "candidate_sha256": self.SHA},
                    {"approved": 1, "candidate_sha256": self.SHA},
                    {"approved": True, "candidate_sha256": 5}):
            with self.assertRaises(MalformedOutput, msg=str(bad)):
                parse_verdict(json.dumps(bad), self.SHA)

    def test_truly_broken_output_is_malformed(self):
        for bad in ("", "no json here", "{broken", '{"approved": true}', "[1, 2]", "```json\n{}\n```",
                    '{"strategies": 1} trailing {"x"', None):
            with self.assertRaises(MalformedOutput, msg=repr(bad)):
                parse_verdict(bad, self.SHA)
        with self.assertRaises(MalformedOutput):
            parse_plan("<think>only reasoning")

    def test_worker_think_with_python_fence_parses_only_the_final_block(self):
        # A python fence inside <think> is stripped with the reasoning; the final block remains.
        draft = "def solve(lines):\n    return 'draft'\n"
        text = f"<think>try this:\n```python\n{draft}```\n</think>\nFinal:\n```python\n{GOOD}```"
        self.assertEqual(parse_candidate(text), GOOD)

    def test_worker_two_python_blocks_after_stripping_is_malformed(self):
        text = f"<think>x</think>```python\n{GOOD}```\nand an alternative:\n```python\n{GOOD}```"
        with self.assertRaises(MalformedOutput):
            parse_candidate(text)

    def test_draft_then_conflicting_final_verdict_is_ambiguous(self):
        draft = {**self.VERDICT, "approved": True}
        final = {**self.VERDICT, "approved": False}
        text = f"Draft:\n```json\n{json.dumps(draft)}\n```\nOn reflection: {json.dumps(final)}"
        with self.assertRaisesRegex(MalformedOutput, "ambiguous"):
            parse_verdict(text, self.SHA)
        other_plan = json.dumps({"strategies": [{"strategy_id": "z", "description": "other"}]})
        with self.assertRaisesRegex(MalformedOutput, "ambiguous"):
            parse_plan(f"{self.PLAN_JSON}\nor maybe\n{other_plan}")

    def test_identical_repeated_verdicts_are_fine(self):
        text = f"{json.dumps(self.VERDICT)}\nTo repeat: {json.dumps(self.VERDICT)}"
        self.assertTrue(parse_verdict(text, self.SHA)["approved"])
        # extras may differ; only the required fields must agree
        text = f'{json.dumps(self.VERDICT)} {json.dumps({**self.VERDICT, "rationale": "again"})}'
        self.assertTrue(parse_verdict(text, self.SHA)["approved"])

    def test_verdict_after_quoted_code_with_dict_literals(self):
        text = ("Reviewing:\n```python\ndef solve(lines):\n    d = {'a': {1: 2}}\n"
                "    return {'by_level': {}, 'by_user': {}}\n```\n"
                f"Verdict: {json.dumps(self.VERDICT)}")
        self.assertTrue(parse_verdict(text, self.SHA)["approved"])

    def test_objects_inside_arrays_are_never_accepted(self):
        with self.assertRaises(MalformedOutput):
            parse_verdict(f"[{json.dumps(self.VERDICT)}]", self.SHA)
        with self.assertRaises(MalformedOutput):
            parse_plan(f'[{{"strategies": {json.dumps(json.loads(self.PLAN_JSON)["strategies"])}}}]')
        # fail closed: an array-wrapped draft next to a top-level answer is ambiguous, not ignored
        text = f"Examples: [{json.dumps({**self.VERDICT, 'approved': False})}] Final: {json.dumps(self.VERDICT)}"
        with self.assertRaises(MalformedOutput):
            parse_verdict(text, self.SHA)

    def test_fail_closed_repros(self):
        inner = {"approved": True, "candidate_sha256": self.SHA}
        reject = {"approved": False, "candidate_sha256": self.SHA, "rationale": "no"}
        final = json.dumps(self.VERDICT)
        cases = {
            "nested in another object": json.dumps({"decision": "reject", "format": inner}),
            "array rejection after prose": f"{{yes}} final: [{json.dumps(reject)}]",
            "NaN rejection": f'{{"approved": false, "candidate_sha256": "{self.SHA}", "x": NaN}} {{yes}} {final}',
            "duplicate-key rejection": f'{{"approved": false, "approved": false, "candidate_sha256": "{self.SHA}"}} {final}',
            "trailing comma rejection": f'{{"approved": false, "candidate_sha256": "{self.SHA}",}} {final}',
            "python-style rejection": f"{{'approved': False, 'candidate_sha256': '{self.SHA}'}} {{yes}} {final}",
        }
        for name, text in cases.items():
            with self.assertRaises(MalformedOutput, msg=name):
                parse_verdict(text, self.SHA)
        bad_plan = f"{{'strategies': []}}\n{self.PLAN_JSON}"
        with self.assertRaises(MalformedOutput):
            parse_plan(bad_plan)

    def test_oversized_and_pathological_replies_fail_fast(self):
        import time
        for text in ("[" * 60000, '{"a":' * 12000, "{" * 60000, "[" * 1_000_000,
                     "x" * 65537 + json.dumps(self.VERDICT)):
            for parse in (lambda t: parse_verdict(t, self.SHA), parse_plan):
                started = time.monotonic()
                with self.assertRaises(MalformedOutput):
                    parse(text)
                self.assertLess(time.monotonic() - started, 1.0, text[:12])

    def test_many_undecodable_openers_are_capped(self):
        with self.assertRaisesRegex(MalformedOutput, "too many"):
            parse_verdict("{x} " * 300 + json.dumps(self.VERDICT), self.SHA)
        self.assertTrue(parse_verdict("{x} " * 100 + json.dumps(self.VERDICT), self.SHA)["approved"])

    def test_stray_closing_think_after_a_brace_is_malformed(self):
        with self.assertRaises(MalformedOutput):
            parse_verdict(f'draft {{"approved": false}}</think>{json.dumps(self.VERDICT)}', self.SHA)
        with self.assertRaises(MalformedOutput):
            parse_candidate(f"{{}}</think>```python\n{GOOD}```")

    def test_candidate_fence_tags(self):
        for tag in ("python", "py", "python3", "Python", "PY", "Python3"):
            self.assertEqual(parse_candidate(f"```{tag}\n{GOOD}```"), GOOD, tag)
        with self.assertRaises(MalformedOutput):
            parse_candidate(f"```javascript\n{GOOD}```")
        with self.assertRaises(MalformedOutput):
            parse_candidate(f"```py\n{GOOD}```\n```python3\n{GOOD}```")

    def test_conflicting_bad_typed_object_is_not_ignored(self):
        text = f'{{"approved": "true", "candidate_sha256": "{self.SHA}"}} {json.dumps(self.VERDICT)}'
        with self.assertRaises(MalformedOutput):
            parse_verdict(text, self.SHA)

    def test_strategy_entries_tolerate_extras_but_return_two_keys(self):
        text = json.dumps({"strategies": [
            {"strategy_id": "a", "description": "one", "rationale": "r", "title": "t"},
            {"strategy_id": "b", "description": "two", "title": "u"}]})
        self.assertEqual(parse_plan(text), [{"strategy_id": "a", "description": "one"},
                                            {"strategy_id": "b", "description": "two"}])
        for bad in ({"strategy_id": 1, "description": "x"}, {"strategy_id": "a"},
                    {"description": "x"}, "text"):
            with self.assertRaises(MalformedOutput, msg=str(bad)):
                parse_plan(json.dumps({"strategies": [bad]}))

    def test_candidate_after_a_quoted_json_block_parses(self):
        text = ('The output looks like:\n```json\n{"by_level": {}, "by_user": {}}\n```\n'
                f"My solution:\n```python\n{GOOD}```\n")
        self.assertEqual(parse_candidate(text), GOOD)
        text = f"```json\n{{}}\n```\n```python\n{GOOD}```\n```json\n[]\n```"
        self.assertEqual(parse_candidate(text), GOOD)

    def test_worker_other_fences_are_allowed(self):
        self.assertEqual(parse_candidate(f"```bash\nrun it\n```\n```python\n{GOOD}```\n```\nout\n```"), GOOD)


if __name__ == "__main__":
    unittest.main()
