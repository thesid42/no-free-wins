import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest import mock

from agent_team import __main__ as cli, analyst, autopilot, live
from agent_team import config as nfw_config
from agent_team.models import FixtureModelClient

HOME = str(Path.home())
LEAKY = f"{analyst.PROJECT_ROOT}/private/repos/x.py and {HOME}/secret failed: baseline too slow"


def proposal(proposal_id="prop1", **changes):
    result = {"proposal_id": proposal_id, "status": "proposal", "questions": [], "suggestions": [],
              "reason": None, "candidates": [], "assumptions": ["size = number of lines"],
              "proposal": {"target": "pkg/csvload.py:load"}, "cost_usd": 0.05}
    result.update(changes)
    return result


def not_feasible():
    return {"proposal_id": "prop1", "status": "not_feasible", "reason": "I/O bound",
            "suggestions": ["batch reads"], "assumptions": [], "cost_usd": 0.02}


def run_result(state, *, events=None, cost=0.5, speedup=None, run_id="run-1"):
    best = {"acceptance": {"median_speedup": speedup, "ci_lower": 3.2}} if speedup else None
    return {"run_id": run_id, "state": state, "rounds": 2, "ledger_final_sha256": "ab" * 32,
            "budget": {"cost_usd": cost}, "events": events or [], "best_response": best}


SCREEN_EVENTS = [
    {"type": "candidate_created", "round": 1, "candidate_id": "worker_a-r1",
     "strategy_id": "single-pass"},
    {"type": "screen_result", "round": 1, "candidate_id": "worker_a-r1", "status": "screened",
     "correctness_passed": True, "screen_median_speedup": 1.4},
    {"type": "verifier_verdict", "round": 2, "candidate_id": "worker_a-r2", "approved": False},
    {"type": "terminal", "round": 2, "state": "rounds_exhausted",
     "reason": "no acceptance after 2 rounds", "last_forwarded_candidate_id": None}]


class Fakes:
    def __init__(self, analyses=(), approvals=(), runs=()):
        self.analyses, self.approvals, self.runs = list(analyses), list(approvals), list(runs)
        self.analyze_calls, self.approve_calls, self.run_calls = [], [], []

    def analyze(self, **kwargs):
        self.analyze_calls.append(kwargs)
        item = self.analyses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def approve(self, proposal_id, **kwargs):
        self.approve_calls.append((proposal_id, kwargs))
        item = self.approvals.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def run(self, **kwargs):
        self.run_calls.append(kwargs)
        item = self.runs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def prepared(session_id):
    return {"status": "prepared", "session_id": session_id, "prepare": {}}


def failed(error="ValueError: target is not deterministic"):
    return {"status": "prepare_failed", "error": error}


class AutopilotCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proposals = Path(self.tmp.name) / "proposals"
        self.runs_root = Path(self.tmp.name) / "runs"
        self.events, self.sleeps = [], []
        self.cfg = nfw_config.load_config()

    def go(self, fakes, **kw):
        kw.setdefault("cfg", self.cfg)
        return autopilot.run_autopilot(
            repo="https://example.invalid/r", prompt="make it faster", session_prefix="job1",
            client=object(), emit=self.events.append, analyze_fn=fakes.analyze,
            approve_fn=fakes.approve, run_fn=fakes.run, now=lambda: "2026-10-17T00:00:00Z",
            proposals_root=self.proposals, runs_root=self.runs_root, sleep=self.sleeps.append, **kw)

    def auto_json(self, proposal_id="prop1"):
        return json.loads((self.proposals / proposal_id / "auto.json").read_text())


class PipelineTests(AutopilotCase):
    def test_happy_path_analyze_prepare_run_verified(self):
        fakes = Fakes([proposal()], [prepared("job1-p1")],
                      [run_result("verified_done", speedup=3.6, cost=0.7)])
        result = self.go(fakes)
        self.assertEqual(result["status"], "verified_done")
        self.assertEqual(result["proposal_id"], "prop1")
        self.assertEqual(result["assumptions"], ["size = number of lines"])
        self.assertEqual(result["attempts"], [{"attempt": 1, "session_id": "job1-p1",
                                               "status": "prepared"}])
        (run,) = result["runs"]
        self.assertEqual((run["session_id"], run["run_id"], run["state"], run["median_speedup"],
                          run["ci"]), ("job1-p1", "run-1", "verified_done", 3.6, 3.2))
        self.assertTrue(run["run_dir"].startswith("artifacts/runs/job1-p1-r1-20261017T000000Z"))
        self.assertEqual(result["final"], {"run_dir": run["run_dir"], "state": "verified_done",
                                           "ledger_final_sha256": "ab" * 32})
        self.assertAlmostEqual(result["total_cost_usd"], 0.75)
        self.assertIn("3.60x", result["report"])
        analysis = fakes.analyze_calls[0]
        self.assertTrue(analysis["autonomous"])
        self.assertEqual((analysis["repo"], analysis["prompt"]), ("https://example.invalid/r",
                                                                  "make it faster"))
        proposal_id, kwargs = fakes.approve_calls[0]
        self.assertEqual((proposal_id, kwargs["session_id"]), ("prop1", "job1-p1"))
        self.assertNotIn("run", kwargs)
        self.assertIsNone(fakes.run_calls[0]["extra_summary"])
        self.assertEqual(fakes.run_calls[0]["session_id"], "job1-p1")
        self.assertEqual(fakes.run_calls[0]["output_dir"].parent, self.runs_root)

    def test_auto_json_is_recorded_privately(self):
        self.go(Fakes([proposal()], [prepared("job1-p1")], [run_result("verified_done")]))
        path = self.proposals / "prop1" / "auto.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        record = self.auto_json()
        self.assertEqual((record["status"], record["session_prefix"]), ("verified_done", "job1"))
        self.assertEqual(record["runs"][0]["state"], "verified_done")
        self.assertEqual(record["limits"], {"max_prepare_attempts": 3, "max_runs": 2,
                                            "max_total_cost_usd": 10})
        self.assertEqual(record["assumptions"], ["size = number of lines"])

    def test_prepare_failed_twice_then_ok_feeds_errors_back_with_p1_p2_p3(self):
        fakes = Fakes([proposal(), proposal(), proposal()],
                      [failed(LEAKY), failed("generator timed out"), prepared("job1-p3")],
                      [run_result("verified_done")])
        result = self.go(fakes)
        self.assertEqual(result["status"], "verified_done")
        self.assertEqual([a["session_id"] for a in result["attempts"]],
                         ["job1-p1", "job1-p2", "job1-p3"])
        self.assertEqual([a["status"] for a in result["attempts"]],
                         ["prepare_failed", "prepare_failed", "prepared"])
        repairs = fakes.analyze_calls[1:]
        self.assertEqual(len(repairs), 2)
        for call in repairs:
            self.assertEqual(call["proposal_id"], "prop1")
            self.assertTrue(call["autonomous"])
            self.assertTrue(call["answer"].startswith("Prepare failed: "))
            self.assertIn("Fix the proposal so prepare succeeds", call["answer"])
            self.assertNotIn("repo", call)
        self.assertIn("baseline too slow", repairs[0]["answer"])
        self.assertNotIn(HOME, repairs[0]["answer"])
        self.assertNotIn(str(analyst.PROJECT_ROOT), repairs[0]["answer"])
        self.assertIn("generator timed out", repairs[1]["answer"])
        self.assertEqual([c[1]["session_id"] for c in fakes.approve_calls],
                         ["job1-p1", "job1-p2", "job1-p3"])
        self.assertEqual(fakes.run_calls[0]["session_id"], "job1-p3")

    def test_prepare_failing_every_time_gives_up_with_the_errors(self):
        fakes = Fakes([proposal()] * 3, [failed("e1"), failed("e2"), failed("e3")])
        result = self.go(fakes)
        self.assertEqual(result["status"], "gave_up")
        self.assertEqual(len(fakes.approve_calls), 3)
        self.assertEqual(len(fakes.analyze_calls), 3)  # analyze + two repairs, none after the last
        self.assertFalse(fakes.run_calls)
        self.assertEqual(result["runs"], [])
        self.assertIsNone(result["final"])
        for text in ("e1", "e2", "e3"):
            self.assertIn(text, result["report"] + json.dumps(self.auto_json()))
        self.assertIn("Gave up", result["report"])

    def test_max_prepare_attempts_override_is_capped(self):
        fakes = Fakes([proposal()], [failed("e1")])
        self.assertEqual(self.go(fakes, max_prepare_attempts=1)["status"], "gave_up")
        self.assertEqual(len(fakes.approve_calls), 1)

    def test_run_one_exhausted_then_lessons_reach_run_two_which_verifies(self):
        fakes = Fakes([proposal()], [prepared("job1-p1")],
                      [run_result("rounds_exhausted", events=SCREEN_EVENTS, cost=1.0),
                       run_result("verified_done", speedup=4.0, cost=1.5, run_id="run-2")])
        result = self.go(fakes)
        self.assertEqual(result["status"], "verified_done")
        self.assertEqual([r["state"] for r in result["runs"]], ["rounds_exhausted", "verified_done"])
        self.assertEqual({r["session_id"] for r in result["runs"]}, {"job1-p1"})
        first, second = fakes.run_calls
        self.assertIsNone(first["extra_summary"])
        lessons = second["extra_summary"]
        self.assertTrue(lessons.startswith("Lessons from the previous attempt: "))
        for needle in ("rounds_exhausted", "no acceptance after 2 rounds", "worker_a-r1",
                       "screen speedup 1.4", "screen_below_bar", "verifier_rejected"):
            self.assertIn(needle, lessons)
        self.assertLessEqual(len(lessons), autopilot.MAX_LESSONS_CHARS + 40)
        self.assertNotEqual(first["output_dir"], second["output_dir"])
        self.assertIn("-r1-", first["output_dir"].name)
        self.assertIn("-r2-", second["output_dir"].name)
        self.assertAlmostEqual(result["total_cost_usd"], 2.55)
        self.assertEqual(self.auto_json()["runs"][1]["lessons_in"], lessons)

    def test_every_run_failing_gives_up_after_max_runs(self):
        fakes = Fakes([proposal()], [prepared("job1-p1")],
                      [run_result("failed_deadline"), run_result("rounds_exhausted")])
        result = self.go(fakes)
        self.assertEqual(result["status"], "gave_up")
        self.assertEqual(len(result["runs"]), 2)
        self.assertEqual(result["final"]["state"], "rounds_exhausted")
        self.assertEqual(self.auto_json()["status"], "gave_up")

    def test_a_raising_run_gives_up(self):
        result = self.go(Fakes([proposal()], [prepared("job1-p1")], [RuntimeError("no key")]))
        self.assertEqual(result["status"], "gave_up")
        self.assertEqual(result["runs"][0]["state"], "error")

    def test_not_feasible_stops_before_prepare(self):
        fakes = Fakes([not_feasible()])
        result = self.go(fakes)
        self.assertEqual(result["status"], "not_feasible")
        self.assertEqual(result["not_feasible"]["suggestions"], ["batch reads"])
        self.assertIn("I/O bound", result["report"])
        self.assertFalse(fakes.approve_calls or fakes.run_calls)

    def test_not_feasible_after_a_repair_is_terminal(self):
        fakes = Fakes([proposal(), not_feasible()], [failed("e1")])
        self.assertEqual(self.go(fakes)["status"], "not_feasible")

    def test_analysis_errors_and_questions_give_up(self):
        error = analyst.AnalysisFailed("analysis budget exhausted", proposal_id="prop1", cost_usd=0.4)
        result = self.go(Fakes([error]))
        self.assertEqual((result["status"], result["proposal_id"]), ("gave_up", "prop1"))
        self.assertAlmostEqual(result["total_cost_usd"], 0.4)
        result = self.go(Fakes([proposal(status="questions")]))
        self.assertEqual(result["status"], "gave_up")

    def test_assumptions_from_a_real_autonomous_analyst_flow_into_the_result(self):
        # the injected analyst reports what analyze(autonomous=True) returns after auto-resolving
        fakes = Fakes([proposal(assumptions=["picked load()", "ASCII input"])],
                      [prepared("job1-p1")], [run_result("verified_done")])
        self.assertEqual(self.go(fakes)["assumptions"], ["picked load()", "ASCII input"])


def outage(proposal_id="prop1", summary="primary model_error: crusoe HTTP 503 No capacity"):
    return analyst.AnalysisFailed("the analyst model failed to give a usable reply (all 3 attempts "
                                  "failed)", proposal_id=proposal_id, cost_usd=0.1, summary=summary)


class AnalystRetryTests(AutopilotCase):
    def test_initial_analyze_is_retried_with_backoff_and_resumes_by_proposal_id(self):
        fakes = Fakes([outage(), outage(), proposal()], [prepared("job1-p1")],
                      [run_result("verified_done")])
        result = self.go(fakes)
        self.assertEqual(result["status"], "verified_done")
        self.assertEqual(self.sleeps, [20, 60])
        first, second, third = fakes.analyze_calls
        self.assertEqual((first["repo"], first["prompt"]), ("https://example.invalid/r", "make it faster"))
        for resumed in (second, third):  # no repo/prompt (no second analysis) and no new answer
            self.assertEqual((resumed["proposal_id"], resumed["answer"]), ("prop1", None))
            self.assertNotIn("repo", resumed)
            self.assertTrue(resumed["autonomous"])
        self.assertAlmostEqual(result["total_cost_usd"], 0.1 + 0.1 + 0.05 + 0.5)
        analyze_events = [(e["status"], e["message"]) for e in self.events if e["step"] == "analyze"]
        self.assertEqual([status for status, _ in analyze_events],
                         ["started", "failed", "started", "failed", "started", "ok"])
        self.assertIn("model_error: crusoe HTTP 503", analyze_events[1][1])
        self.assertIn("retry 1 of 2 in 20s", analyze_events[1][1])
        self.assertIn("retry 2 of 2 in 60s", analyze_events[3][1])
        self.assertEqual(self.auto_json()["analyst_retries"][0]["delay_seconds"], 20)

    def test_a_repair_answer_is_retried_without_resending_it(self):
        fakes = Fakes([proposal(), outage(summary="retry malformed: not json"), proposal()],
                      [failed("e1"), prepared("job1-p2")], [run_result("verified_done")])
        result = self.go(fakes)
        self.assertEqual(result["status"], "verified_done")
        _, repair, retry = fakes.analyze_calls
        self.assertTrue(repair["answer"].startswith("Prepare failed: "))
        self.assertEqual((retry["proposal_id"], retry["answer"]), ("prop1", None))
        repair_events = [e for e in self.events if e["step"] == "repair"]
        self.assertEqual([e["status"] for e in repair_events], ["started", "failed", "started", "ok"])
        self.assertIn("malformed", repair_events[1]["message"])
        self.assertEqual(self.sleeps, [20])

    def test_gives_up_only_after_the_retries_are_exhausted(self):
        fakes = Fakes([outage(), outage(), outage()])
        result = self.go(fakes)
        self.assertEqual(result["status"], "gave_up")
        self.assertEqual(len(fakes.analyze_calls), 3)
        self.assertEqual(self.sleeps, [20, 60])
        self.assertFalse(fakes.approve_calls)
        self.assertIn("crusoe HTTP 503", self.auto_json()["message"])
        self.cfg["auto"]["max_analyst_retries"] = 0
        self.sleeps.clear()
        fakes = Fakes([outage()])
        self.assertEqual(self.go(fakes)["status"], "gave_up")
        self.assertEqual((len(fakes.analyze_calls), self.sleeps), (1, []))

    def test_retries_respect_the_config_cap_and_the_spend_guard(self):
        self.cfg["auto"]["max_analyst_retries"] = 4
        fakes = Fakes([outage()] * 5)
        self.assertEqual(self.go(fakes)["status"], "gave_up")
        self.assertEqual(self.sleeps, [20, 60, 60, 60])
        self.cfg["auto"]["max_total_cost_usd"] = 1.2  # outages cost 0.1 each: the guard trips first
        self.sleeps.clear()
        fakes = Fakes([outage()] * 5)
        self.assertEqual(self.go(fakes)["status"], "gave_up")
        self.assertLess(len(fakes.analyze_calls), 5)
        self.assertIn("spend guard", self.auto_json()["message"])

    def test_other_analyst_errors_are_not_retried(self):
        fakes = Fakes([analyst.ProposalError("no proposal x")])
        self.assertEqual(self.go(fakes)["status"], "gave_up")
        self.assertEqual((len(fakes.analyze_calls), self.sleeps), (1, []))


class GuardTests(AutopilotCase):
    def test_spend_guard_stops_before_an_over_budget_run(self):
        self.cfg["auto"]["max_total_cost_usd"] = 5.5  # analysis 1 + run 5 fits only while spend is tiny
        fakes = Fakes([proposal(cost_usd=0.9)], [prepared("job1-p1")], [run_result("verified_done")])
        result = self.go(fakes)
        self.assertEqual(result["status"], "gave_up")
        self.assertFalse(fakes.run_calls)
        self.assertIn("spend guard", self.auto_json()["message"])

    def test_spend_guard_stops_the_second_run(self):
        self.cfg["auto"]["max_total_cost_usd"] = 10
        fakes = Fakes([proposal()], [prepared("job1-p1")],
                      [run_result("rounds_exhausted", cost=5.2), run_result("verified_done")])
        result = self.go(fakes)
        self.assertEqual(result["status"], "gave_up")
        self.assertEqual(len(fakes.run_calls), 1)
        self.assertIn("spend guard", self.auto_json()["message"])

    def test_spend_guard_stops_before_an_over_budget_analysis(self):
        self.cfg["auto"]["max_total_cost_usd"] = 0.5
        fakes = Fakes([proposal()])
        self.assertEqual(self.go(fakes)["status"], "gave_up")
        self.assertFalse(fakes.analyze_calls)

    def test_total_cost_is_hard_capped_at_25(self):
        self.cfg["auto"]["max_total_cost_usd"] = 25
        self.assertEqual(self.go(Fakes([proposal()], [prepared("job1-p1")],
                                       [run_result("verified_done")]))["status"], "verified_done")
        bad = nfw_config.load_config()
        bad["auto"]["max_total_cost_usd"] = 26
        with self.assertRaises(nfw_config.ConfigError):
            nfw_config.validate_config(bad)

    def test_bad_session_prefix_is_rejected_before_anything_runs(self):
        for prefix in ("", "a/b", "a b", "x" * 101, "../x"):
            with self.assertRaises(ValueError, msg=prefix):
                autopilot.run_autopilot(repo="r", prompt="p", session_prefix=prefix, client=object(),
                                        cfg=self.cfg, analyze_fn=Fakes().analyze)
        self.assertEqual(len(f"{'x' * 100}-p5"), 103)  # still inside live's 128-character session id
        live.check_session_id(f"{'x' * 100}-p5")


class ProgressTests(AutopilotCase):
    def test_progress_lines_are_valid_json_with_sanitized_paths(self):
        fakes = Fakes([proposal(), proposal()], [failed(LEAKY), prepared("job1-p2")],
                      [run_result("verified_done")])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = autopilot.run_autopilot(
                repo="r", prompt="p", session_prefix="job1", client=object(), cfg=self.cfg,
                analyze_fn=fakes.analyze, approve_fn=fakes.approve, run_fn=fakes.run,
                now=lambda: "2026-10-17T00:00:00Z", proposals_root=self.proposals,
                runs_root=self.runs_root, sleep=self.sleeps.append)
        lines = err.getvalue().splitlines()
        self.assertTrue(lines and all(line.startswith("NFW_PROGRESS ") for line in lines))
        events = [json.loads(line[len("NFW_PROGRESS "):]) for line in lines]
        keys = {"ts", "step", "attempt", "status", "message", "proposal_id", "session_id",
                "run_dir", "run_id"}
        for event in events:
            self.assertEqual(set(event), keys)
            self.assertIn(event["step"], autopilot.STEPS)
            self.assertIn(event["status"], ("started", "ok", "failed"))
        self.assertEqual([e["step"] for e in events],
                         ["analyze", "analyze", "prepare", "prepare", "repair", "repair", "prepare",
                          "prepare", "run", "run", "done"])
        text = "\n".join(lines)
        self.assertNotIn(HOME, text)
        self.assertNotIn(str(analyst.PROJECT_ROOT), text)
        failure = next(e for e in events if e["step"] == "prepare" and e["status"] == "failed")
        self.assertIn("<project>", failure["message"])
        self.assertIn("~/secret", failure["message"])
        run_started = next(e for e in events if e["step"] == "run" and e["status"] == "started")
        self.assertTrue(run_started["run_dir"].startswith("artifacts/runs/job1-p2-r1-"))
        self.assertEqual((run_started["session_id"], run_started["proposal_id"]), ("job1-p2", "prop1"))
        self.assertEqual(events[-1]["status"], "ok")
        self.assertEqual(events[-1]["run_id"], "run-1")
        self.assertEqual(result["status"], "verified_done")

    def test_print_progress_writes_one_compact_line_to_stderr(self):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            autopilot.print_progress({"step": "run", "message": "a b"})
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), 'NFW_PROGRESS {"message":"a b","step":"run"}\n')

    def test_failed_run_end_is_a_failed_done_event(self):
        self.go(Fakes([not_feasible()]))
        self.assertEqual((self.events[-1]["step"], self.events[-1]["status"]), ("done", "failed"))
        self.assertIn("not_feasible", self.events[-1]["message"])


class LessonsTests(unittest.TestCase):
    def test_lessons_hold_no_source_and_stay_short(self):
        events = SCREEN_EVENTS + [{"type": "candidate_created", "round": 2, "candidate_id": "c" * 500,
                                   "strategy_id": "s" * 500}] * 40
        lessons = autopilot.build_lessons({"state": "rounds_exhausted", "events": events,
                                           "best_response": {"status": "bar_not_met", "feedback_public": [
                                               {"code": "too_slow", "summary": "SECRET source"}]}})
        self.assertLessEqual(len(lessons), autopilot.MAX_LESSONS_CHARS + len(autopilot.LESSONS_HEADER))
        self.assertIn("too_slow", lessons)
        self.assertNotIn("SECRET", lessons)

    def test_lessons_survive_empty_runs(self):
        self.assertIn("ended failed_plan", autopilot.build_lessons({"state": "failed_plan"}))


class RunWiringTests(unittest.TestCase):
    def test_build_live_run_appends_extra_summary_to_the_task_summary(self):
        ids = ["nvidia/NVIDIA-Nemotron-3-Super-120B-A12B", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
               "zai-org/GLM-5.3-Flash", "deepseek-ai/Deepseek-V4-Flash"]
        context = ("repo-task", "BASE SUMMARY", {"type": "run_label", "label": "x"},
                   {"task_id": "repo-task", "kind": "repo", "include": "full", "n_files": 1,
                    "bundle_bytes": 1})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(live, "make_preflight", lambda session_id: (lambda: None)), \
                mock.patch.object(live, "_task_context", return_value=context):
            for extra, expected in ((None, "BASE SUMMARY"),
                                    ("Lessons from the previous attempt: x",
                                     "BASE SUMMARY\n\nLessons from the previous attempt: x")):
                orchestrator, _ = live.build_live_run(
                    session_id="s", output_dir=Path(tmp) / str(extra is None), client=FixtureModelClient({}),
                    available_ids=ids, evaluate=lambda request: None, extra_summary=extra)
                self.assertEqual(orchestrator.task_summary, expected)

    def test_default_run_goes_through_run_live_with_config_defaults_and_lessons(self):
        result = types.SimpleNamespace(
            run_id="run-9", state="rounds_exhausted", rounds=2, ledger_path="l", events=[{"type": "t", "last_forwarded_candidate_id": None}],
            ledger_final_sha256="ab", budget={"cost_usd": 1.0}, best_response=None)
        orchestrator = mock.Mock()
        orchestrator.run.return_value = result
        with mock.patch.object(live, "build_live_run", return_value=(orchestrator, {"models": {}})) as build, \
                mock.patch.object(cli, "verify_ledger", return_value=[{"sha256": "ab"}]):
            out = autopilot.default_run(session_id="s1", output_dir=Path("/x/y"),
                                        extra_summary="Lessons from the previous attempt: z")
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["extra_summary"], "Lessons from the previous attempt: z")
        self.assertEqual((kwargs["session_id"], kwargs["output_dir"]), ("s1", Path("/x/y")))
        self.assertEqual((kwargs["deadline_seconds"], kwargs["max_rounds"], kwargs["max_cost_usd"]),
                         (1800, 2, 5))
        self.assertFalse(kwargs["inject_failures"])
        self.assertEqual((out["state"], out["events"], out["budget"]),
                         ("rounds_exhausted", [{"type": "t", "last_forwarded_candidate_id": None}],
                          {"cost_usd": 1.0}))

    def test_plain_run_live_passes_no_extra_summary(self):
        orchestrator = mock.Mock()
        orchestrator.run.return_value = types.SimpleNamespace(
            run_id="r", state="x", rounds=1, ledger_path="l",
            events=[{"last_forwarded_candidate_id": None}], ledger_final_sha256="a",
            budget={}, best_response=None)
        args = types.SimpleNamespace(session_id="s", output=Path("o"), deadline=10, max_rounds=1,
                                     max_cost_usd=1, max_tokens=1000, inject_failures=False)
        with mock.patch.object(live, "build_live_run", return_value=(orchestrator, {"models": {}})) as build, \
                mock.patch.object(cli, "verify_ledger", return_value=[]):
            cli.run_live(args)
        self.assertIsNone(build.call_args.kwargs["extra_summary"])


class CliTests(unittest.TestCase):
    def call(self, argv, outcome=None):
        out, err = io.StringIO(), io.StringIO()
        patcher = mock.patch.object(autopilot, "run_autopilot", side_effect=(
            outcome if isinstance(outcome, Exception) else None), return_value=outcome)
        with patcher as fake, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["auto", *argv])
        return code, out.getvalue(), err.getvalue(), fake

    def test_exit_codes_follow_the_final_status(self):
        for status, expected in (("verified_done", 0), ("not_feasible", 3), ("gave_up", 1)):
            code, out, _, fake = self.call(["--repo", "r", "--prompt", "p", "--session-prefix", "job"],
                                           {"status": status, "report": "x"})
            self.assertEqual(code, expected, status)
            self.assertEqual(json.loads(out)["status"], status)
            self.assertEqual(fake.call_args.kwargs["session_prefix"], "job")

    def test_default_prefix_and_limits_are_passed_through(self):
        code, _, _, fake = self.call(["--repo", "r", "--prompt", "p", "--max-prepare-attempts", "2",
                                      "--max-runs", "1"], {"status": "verified_done"})
        kwargs = fake.call_args.kwargs
        self.assertEqual((code, kwargs["max_prepare_attempts"], kwargs["max_runs"]), (0, 2, 1))
        self.assertRegex(kwargs["session_prefix"], r"^auto-\d{8}T\d{6}Z-[0-9a-f]{6}$")
        self.assertRegex(kwargs["session_prefix"], autopilot.SESSION_PREFIX.pattern)

    def test_usage_errors_exit_2_without_running(self):
        bad = [["--repo", "r", "--prompt", "p", "--session-prefix", "a/b"],
               ["--repo", "r", "--prompt", "p", "--session-prefix", "x" * 101],
               ["--repo", " ", "--prompt", "p"],
               ["--repo", "r", "--prompt", " "],
               ["--repo", "r", "--prompt", "x" * 4001],
               ["--repo", "r", "--prompt", "p", "--max-prepare-attempts", "6"],
               ["--repo", "r", "--prompt", "p", "--max-prepare-attempts", "0"],
               ["--repo", "r", "--prompt", "p", "--max-runs", "4"]]
        for argv in bad:
            code, out, _, fake = self.call(argv, {"status": "verified_done"})
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, "")
            fake.assert_not_called()

    def test_missing_required_flags_exit_2_via_argparse(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main(["auto", "--repo", "r"])
        self.assertEqual(caught.exception.code, 2)

    def test_unexpected_errors_are_one_line_and_exit_1(self):
        code, out, err, _ = self.call(["--repo", "r", "--prompt", "p"],
                                      nfw_config.ConfigError("cannot read config file"))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("ConfigError", err)
        self.assertEqual(len(err.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
