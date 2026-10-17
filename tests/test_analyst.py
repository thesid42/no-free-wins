import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from agent_team import __main__ as cli, analyst, live
from agent_team import config as nfw_config
from agent_team.budget import Budget
from agent_team.calls import ModelOrder
from agent_team.models import FixtureModelClient, MalformedOutput, ModelError
from optimizer_core import sessions

ROOT = Path(__file__).resolve().parents[1]
PRIMARY, FALLBACK = "fixture/analyst-primary", "fixture/analyst-fallback"
ORDER = ModelOrder(PRIMARY, FALLBACK)
PROMPT = "make the CSV loader faster"
GENERATOR = ("import random\n\n\ndef generate_input(seed, size):\n"
             "    rng = random.Random(seed)\n    return [str(rng.randrange(100)) for _ in range(size)]\n")
TARGET_SOURCE = "def load(rows):\n    return rows\n" + "# pad\n" * 2000
LIVE_IDS = [
    "deepseek-ai/Deepseek-V4-Flash", "deepseek-ai/DeepSeek-V4.1-Flash", "openai/gpt-oss-120b", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B", "zai-org/GLM-5.3-Flash"]


def proposal_reply(**changes):
    body = {"status": "proposal", "target": "pkg/csvload.py:load", "generator_source": GENERATOR,
            "timing_size": 20000, "correctness_sizes": [0, 1, 5], "include": "package",
            "description": "Takes a list of CSV lines, returns parsed rows.",
            "hints": ["avoid per-line regex", "use str.split"], "risks": ["quoted fields"]}
    body.update(changes)
    return json.dumps(body)


def questions_reply(*questions):
    return json.dumps({"status": "questions", "questions": list(questions) or ["Which loader?"],
                       "candidates": ["pkg/csvload.py:load"]})


def infeasible_reply():
    return json.dumps({"status": "not_feasible", "reason": "the loader is I/O bound",
                       "suggestions": ["batch the reads", "extract the parsing into a pure function"]})


class ListingClient(FixtureModelClient):
    def list_models(self):
        return list(LIVE_IDS)


def fake_repo_task(root, validation_errors=()):
    """Stand-in for optimizer_core.repo_task; validation_errors are raised one per call."""
    module = types.ModuleType("optimizer_core.repo_task")
    module.errors = list(validation_errors)
    module.validated = []
    module.fetched = []

    def fetch_repo(repo, dest_root="private/repos"):
        module.fetched.append(repo)
        return Path(root), {"kind": "local", "path": str(root)}

    def repo_map(path, *, include="full", max_source_bytes=120_000):
        return {"files": [{"path": "pkg/csvload.py", "bytes": len(TARGET_SOURCE),
                           "functions": [{"qualname": "load", "args": ["rows"], "lineno": 1,
                                          "doc": "Load rows."}]}],
                "sources": {"pkg/csvload.py": TARGET_SOURCE}, "truncated": False}

    def validate_proposal(path, proposal):
        module.validated.append(proposal)
        if module.errors:
            raise ValueError(module.errors.pop(0))
        return {"target": proposal["target"]}

    module.fetch_repo, module.repo_map, module.validate_proposal = (
        fetch_repo, repo_map, validate_proposal)
    return module


class AnalystCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.proposals = Path(self.tmp.name) / "proposals"
        self.snapshots = Path(self.tmp.name) / "snapshots"
        self.ids = iter(["prop1", "prop2", "prop3"])

    def use_repo_task(self, **kw):
        self.repo_task = fake_repo_task(self.repo, **kw)
        patcher = mock.patch.dict(sys.modules, {"optimizer_core.repo_task": self.repo_task})
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, script, *, validation_errors=(), cfg=None, **kw):
        self.use_repo_task(validation_errors=validation_errors)
        self.client = FixtureModelClient({PRIMARY: list(script)})
        kw.setdefault("budget", Budget())
        return analyst.analyze(
            repo="https://example.invalid/r", prompt=PROMPT, client=self.client, order=ORDER,
            cfg=cfg, proposals_root=self.proposals, snapshot_root=self.snapshots, new_id=lambda: next(self.ids),
            now=lambda: "2026-10-17T00:00:00Z", **kw)

    def cont(self, script, answer, proposal_id="prop1", **kw):
        self.client = FixtureModelClient({PRIMARY: list(script)})
        kw.setdefault("budget", Budget())
        return analyst.analyze(
            proposal_id=proposal_id, answer=answer, client=self.client, order=ORDER,
            proposals_root=self.proposals, now=lambda: "2026-10-17T00:00:01Z", **kw)

    def state(self, proposal_id="prop1"):
        return json.loads((self.proposals / proposal_id / "state.json").read_text())

    def user_text(self, call=-1):
        return self.client.calls[call][2][1]["content"]


class ProposalTests(AnalystCase):
    def test_accepted_proposal_writes_private_state_and_cli_format_file(self):
        result = self.start([proposal_reply()])
        self.assertEqual(result["status"], "proposal")
        self.assertEqual(result["proposal_id"], "prop1")
        proposal = result["proposal"]
        self.assertEqual((proposal["target"], proposal["include"]), ("pkg/csvload.py:load", "package"))
        self.assertEqual(proposal["sizes"], {"timing_size": 20000, "correctness_sizes": [0, 1, 5]})
        self.assertEqual(proposal["risks"], ["quoted fields"])
        self.assertEqual(proposal["hints"], ["avoid per-line regex", "use str.split"])
        self.assertEqual(proposal["generator_source"], GENERATOR)
        self.assertLessEqual(len(result["target_source_excerpt"].encode()), 4096)
        self.assertTrue(result["target_source_excerpt"].startswith("def load"))
        directory = self.proposals / "prop1"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for name in ("state.json", "proposal.json"):
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600, name)
        self.assertEqual(result["proposal_path"], str(directory / "proposal.json"))
        document = json.loads((directory / "proposal.json").read_text())
        self.assertEqual(set(document), {"repo", "root", "origin_info", "proposal"})
        self.assertEqual(document["origin_info"]["kind"], "local")
        self.assertEqual(document["origin_info"]["path"], str(self.repo.resolve()))
        self.assertEqual(document["root"], document["origin_info"]["snapshot"])  # outside the project
        # exactly the keys validate_proposal accepts: model-only fields (risks) stay out
        self.assertEqual(set(document["proposal"]),
                         {"target", "generator_source", "timing_size", "correctness_sizes", "include",
                          "instruction", "hints", "description"})
        self.assertEqual(document["proposal"]["instruction"], PROMPT)
        self.assertEqual(self.repo_task.validated, [document["proposal"]])
        state = self.state()
        self.assertEqual((state["status"], state["prompt"], state["history"]), ("proposal", PROMPT, []))
        self.assertEqual(state["repo"], "https://example.invalid/r")
        self.assertEqual(state["reply"]["risks"], ["quoted fields"])
        self.assertEqual(self.repo_task.fetched, ["https://example.invalid/r"])

    def test_config_defaults_fill_omitted_fields(self):
        reply = json.dumps({"status": "proposal", "target": "pkg/csvload.py:load",
                            "generator_source": GENERATOR})
        result = self.start([reply])
        defaults = nfw_config.load_config()["repo"]
        self.assertEqual(result["proposal"]["include"], defaults["include"])
        self.assertEqual(result["proposal"]["sizes"],
                         {"timing_size": defaults["timing_size"],
                          "correctness_sizes": defaults["correctness_sizes"]})

    def test_prompt_carries_constraints_repo_and_defaults(self):
        self.start([proposal_reply()])
        system, user = (m["content"] for m in self.client.calls[0][2])
        for needle in ("deterministic", "NO floats", "1 MiB", "0.1 to 0.5 second", "600 KB",
                       "size = number of lines", "EVERY input-handling branch", "generate_input",
                       "JSON object", "not_feasible", "questions"):
            self.assertIn(needle, system)
        self.assertIn(PROMPT, user)
        self.assertIn("pkg/csvload.py", user)
        self.assertIn("def load(rows)", user)
        self.assertEqual(self.client.calls[0][1], "analyst")

    def test_default_order_and_budget_come_from_config_and_the_listing(self):
        self.use_repo_task()
        client = ListingClient({
            "deepseek-ai/DeepSeek-V4.1-Flash": [proposal_reply()]})
        result = analyst.analyze(repo="r", prompt=PROMPT, client=client, proposals_root=self.proposals,
                                 snapshot_root=self.snapshots, new_id=lambda: "prop1")
        self.assertEqual(result["status"], "proposal")
        self.assertEqual(client.calls[0][0], "deepseek-ai/DeepSeek-V4.1-Flash")

    def test_new_analysis_needs_repo_and_prompt_and_continuation_an_answer(self):
        self.use_repo_task()
        client = FixtureModelClient({})
        for kw in ({"repo": "r"}, {"prompt": "p"}, {"repo": "r", "prompt": " "},
                   {"repo": "r", "prompt": "x" * 4001}, {"proposal_id": "p", "answer": ""},
                   {"proposal_id": "p", "answer": "a", "prompt": "q"},
                   {"proposal_id": "p", "answer": "x" * 4001}):
            with self.assertRaises(ValueError, msg=kw):
                analyst.analyze(client=client, order=ORDER, budget=Budget(),
                                proposals_root=self.proposals, snapshot_root=self.snapshots, **kw)
        self.assertEqual(client.calls, [])


class SnapshotTests(AnalystCase):
    def populate(self):
        files = {"pkg/__init__.py": "", "pkg/csvload.py": TARGET_SOURCE, "nofreewins.json": "{}",
                 "README.md": "doc", "data.csv": "a,b", "pkg/notes.txt": "x",
                 "tests/test_a.py": "x", "test/b.py": "x", ".hidden/c.py": "x", "venv/d.py": "x",
                 ".git/e.py": "x", "build/f.py": "x", "dist/g.py": "x", "pkg/__pycache__/h.py": "x"}
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (self.repo / "link.py").symlink_to(self.repo / "pkg/csvload.py")
        (self.repo / "linkdir").symlink_to(self.repo / "pkg")
        return files

    def copied(self, root):
        return sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*") if p.is_file())

    def test_outside_repo_is_snapshotted_with_only_safe_files(self):
        self.populate()
        result = self.start([proposal_reply()])
        document = json.loads(Path(result["proposal_path"]).read_text())
        snapshot = Path(document["root"])
        self.assertEqual(snapshot.parent, self.snapshots)
        self.assertRegex(snapshot.name, r"^local-[0-9a-f]{12}-[0-9a-f]{8}$")
        self.assertEqual(document["origin_info"], {"kind": "local", "path": str(self.repo.resolve()),
                                                   "snapshot": str(snapshot)})
        self.assertEqual(self.copied(snapshot),
                         ["nofreewins.json", "pkg/__init__.py", "pkg/csvload.py"])
        self.assertEqual((snapshot / "pkg/csvload.py").read_text(), TARGET_SOURCE)
        self.assertFalse((snapshot / "link.py").exists())
        self.assertFalse((snapshot / "linkdir").exists())
        for directory in (snapshot, snapshot / "pkg"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(self.state()["root"], str(snapshot))
        self.assertTrue((self.repo / "pkg/csvload.py").exists())  # the original is untouched

    def test_file_count_cap_leaves_nothing_behind(self):
        for index in range(analyst.SNAPSHOT_MAX_FILES + 1):
            (self.repo / f"m{index}.py").write_text("")
        with self.assertRaisesRegex(ValueError, "more than 2000 Python files"):
            self.start([proposal_reply()])
        self.assertEqual(list(self.snapshots.glob("*")) if self.snapshots.exists() else [], [])
        self.assertEqual(self.client.calls, [])

    def test_byte_cap(self):
        (self.repo / "big.py").write_text("#" * (analyst.SNAPSHOT_MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "8 MiB"):
            self.start([proposal_reply()])

    def test_repo_inside_the_project_is_used_in_place(self):
        self.use_repo_task()
        inside = ROOT / "examples" / "textstats"
        self.repo_task.fetch_repo = lambda repo, dest_root="private/repos": (
            inside, {"kind": "local", "path": str(inside)})
        self.client = FixtureModelClient({PRIMARY: [proposal_reply()]})
        result = analyst.analyze(repo=str(inside), prompt=PROMPT, client=self.client, order=ORDER,
                                 budget=Budget(), proposals_root=self.proposals,
                                 snapshot_root=self.snapshots, new_id=lambda: "prop1")
        document = json.loads(Path(result["proposal_path"]).read_text())
        self.assertEqual(document["root"], str(inside))
        self.assertEqual(document["origin_info"], {"kind": "local", "path": str(inside)})
        self.assertFalse(self.snapshots.exists())

    def test_clone_destination_is_absolute_whatever_the_cwd(self):
        self.use_repo_task()
        seen = []
        inside = ROOT / "examples" / "textstats"

        def fetch(repo, dest_root="private/repos"):
            seen.append(dest_root)
            return inside, {"kind": "github", "url": "https://github.com/o/n"}
        self.repo_task.fetch_repo = fetch
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        cwd = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, cwd)
        for kw in ({}, {"snapshot_root": self.snapshots}):
            kw.setdefault("snapshot_root", analyst.DEFAULT_SNAPSHOT_ROOT)
            analyst.analyze(repo="https://github.com/o/n", prompt=PROMPT, order=ORDER, budget=Budget(),
                            client=FixtureModelClient({PRIMARY: [proposal_reply()]}),
                            proposals_root=self.proposals, new_id=lambda: next(self.ids), **kw)
        self.assertEqual(seen, [str(ROOT / "private" / "repos"), str(self.snapshots)])

    def test_the_answer_cap_and_the_prompt_render_cut_agree(self):
        self.assertEqual(analyst.MAX_ANSWER_CHARS, 4000)
        long_answer = "a" * 3999 + "Z"
        messages = analyst.analyst_messages(
            PROMPT, {"files": [], "sources": {}}, [{"question": "q", "answer": long_answer}])
        self.assertIn("A: " + long_answer, messages[1]["content"])

    def test_github_clones_are_not_snapshotted(self):
        self.use_repo_task()
        clone = ROOT / "private" / "repos" / "abc-123"
        self.repo_task.fetch_repo = lambda repo, dest_root="private/repos": (
            clone, {"kind": "github", "url": "https://github.com/o/n"})
        self.assertEqual(analyst._inside(clone, analyst.PROJECT_ROOT), True)
        self.assertEqual(analyst._inside(self.repo, analyst.PROJECT_ROOT), False)


class QuestionTests(AnalystCase):
    def test_questions_then_answer_then_proposal(self):
        first = self.start([questions_reply("Which loader?", "Which file format?")])
        self.assertEqual(first["status"], "questions")
        self.assertEqual(first["questions"], ["Which loader?", "Which file format?"])
        self.assertEqual(first["candidates"], ["pkg/csvload.py:load"])
        self.assertIsNone(first["proposal"])
        self.assertFalse((self.proposals / "prop1" / "proposal.json").exists())
        second = self.cont([proposal_reply()], "the one in csvload, plain CSV")
        self.assertEqual((second["status"], second["proposal_id"]), ("proposal", "prop1"))
        self.assertEqual(second["questions"], [])
        sent = self.user_text()
        self.assertIn("1. Which loader?", sent)
        self.assertIn("2. Which file format?", sent)
        self.assertIn("A: the one in csvload, plain CSV", sent)
        state = self.state()
        self.assertEqual(state["question_rounds"], 1)
        self.assertEqual(len(state["history"]), 1)
        self.assertTrue((self.proposals / "prop1" / "proposal.json").exists())

    def test_question_cap_returns_not_feasible_and_never_loops_forever(self):
        cfg = nfw_config.load_config()
        cfg["analysis"]["max_question_rounds"] = 2
        self.start([questions_reply("q1")], cfg=cfg)
        self.cont([questions_reply("q2")], "a1", cfg=cfg)
        result = self.cont([questions_reply("q3")], "a2", cfg=cfg)
        self.assertEqual(result["status"], "not_feasible")
        self.assertEqual(result["reason"], "too many open questions")
        self.assertEqual(result["suggestions"], ["q3"])
        self.assertEqual(self.state()["status"], "not_feasible")

    def test_answer_after_a_proposal_is_feedback_and_removes_the_old_file(self):
        self.start([proposal_reply()])
        result = self.cont([questions_reply("Which exact column?")], "use the second column instead")
        self.assertEqual(result["status"], "questions")
        self.assertIn("feedback on the proposal for pkg/csvload.py:load", self.user_text())
        self.assertFalse((self.proposals / "prop1" / "proposal.json").exists())
        self.assertEqual(self.state()["question_rounds"], 0)


def assumption_reply(*assumptions, **changes):
    return proposal_reply(assumptions=list(assumptions) or ["size = number of lines"], **changes)


class AutonomousTests(AnalystCase):
    def test_autonomous_prompt_forbids_questions_and_plain_prompt_does_not(self):
        self.start([proposal_reply()], autonomous=True)
        system = self.client.calls[0][2][0]["content"]
        self.assertIn("AUTONOMOUS MODE", system)
        self.assertIn("Do NOT reply with status \"questions\"", system)
        self.start([proposal_reply()])
        self.assertNotIn("AUTONOMOUS MODE", self.client.calls[0][2][0]["content"])

    def test_assumptions_are_recorded_and_appended_to_the_description_only(self):
        result = self.start([assumption_reply("the loader means pkg.csvload.load", "ASCII input")],
                            autonomous=True)
        self.assertEqual(result["assumptions"], ["the loader means pkg.csvload.load", "ASCII input"])
        self.assertEqual(self.state()["assumptions"], result["assumptions"])
        self.assertGreaterEqual(result["cost_usd"], 0)
        saved = json.loads((self.proposals / "prop1" / "proposal.json").read_text())["proposal"]
        self.assertNotIn("assumptions", saved)
        self.assertTrue(saved["description"].startswith("Takes a list of CSV lines"))
        self.assertTrue(saved["description"].endswith(
            "Assumptions: the loader means pkg.csvload.load; ASCII input"))
        self.assertEqual(self.repo_task.validated[-1]["description"], saved["description"])

    def test_long_assumptions_are_trimmed_into_the_description_cap(self):
        many = [("x" * 299 + str(n)) for n in range(10)]
        result = self.start([assumption_reply(*many, description="d" * 1900)], autonomous=True)
        self.assertEqual(len(result["assumptions"]), 10)
        saved = json.loads((self.proposals / "prop1" / "proposal.json").read_text())["proposal"]
        self.assertEqual(len(saved["description"]), analyst.MAX_DESCRIPTION_CHARS)
        reply = analyst.parse_analyst_reply(assumption_reply(*many, "e" * 900, "f"))
        self.assertEqual(len(reply["assumptions"]), 10)
        self.assertTrue(all(len(item) <= 300 for item in reply["assumptions"]))

    def test_questions_are_reasked_once_then_a_proposal_is_taken(self):
        result = self.start([questions_reply("Which loader?"), assumption_reply("picked load()")],
                            autonomous=True)
        self.assertEqual(result["status"], "proposal")
        self.assertEqual(result["assumptions"], ["picked load()"])
        self.assertIn("Do not ask; choose and record assumptions", self.user_text())
        self.assertIn("1. Which loader?", self.user_text())
        self.assertEqual(self.state()["question_rounds"], 1)

    def test_repeated_questions_are_answered_with_best_judgment_until_the_cap(self):
        script = [questions_reply("q1"), questions_reply("q2"), assumption_reply("guess")]
        result = self.start(script, autonomous=True)
        self.assertEqual(result["status"], "proposal")
        self.assertIn("Use your best judgment and record the assumption.", self.user_text())
        self.assertEqual(self.state()["question_rounds"], 2)
        cfg = nfw_config.load_config()
        cfg["analysis"]["max_question_rounds"] = 2
        self.ids = iter(["prop2"])
        result = self.start([questions_reply("q1")] * 3, autonomous=True, cfg=cfg)
        self.assertEqual((result["status"], result["reason"]), ("not_feasible",
                                                                "too many open questions"))

    def test_not_feasible_stays_terminal_and_validation_needs_input_is_auto_fixed(self):
        result = self.start([infeasible_reply()], autonomous=True)
        self.assertEqual(result["status"], "not_feasible")
        self.ids = iter(["prop2"])
        result = self.start([assumption_reply(), assumption_reply(), assumption_reply("fixed")],
                            validation_errors=["bad generator", "still bad"], autonomous=True)
        self.assertEqual(result["status"], "proposal")
        self.assertIn("Fix exactly the validation error", self.user_text())

    def test_assumptions_accumulate_across_a_repair_continuation(self):
        self.start([assumption_reply("first")], autonomous=True)
        with mock.patch.object(analyst, "_run_prepare", return_value=(1, "", "baseline too slow")):
            analyst.approve("prop1", session_id="s1", proposals_root=self.proposals)
        result = self.cont([assumption_reply("second")], "Prepare failed: baseline too slow",
                           autonomous=True)
        self.assertEqual(result["status"], "proposal")
        self.assertEqual(result["assumptions"], ["first", "second"])
        self.assertIn("baseline too slow", self.user_text())

    def test_analysis_failure_carries_the_cost(self):
        budget = Budget(max_attempts=0)
        with self.assertRaises(analyst.AnalysisFailed) as caught:
            self.start([proposal_reply()], autonomous=True, budget=budget)
        self.assertEqual(caught.exception.cost_usd, 0.0)


class FailureLogTests(AnalystCase):
    FAILING = {PRIMARY: [ModelError(f"crusoe HTTP 503 No capacity at {analyst.PROJECT_ROOT}/x"),
                         "not json at all"],
               FALLBACK: [ModelError("crusoe HTTP 503 again " + "z" * 400)]}

    def fail(self, answer=None):
        self.client = FixtureModelClient(self.FAILING)
        with self.assertRaises(analyst.AnalysisFailed) as caught:
            if answer is None:
                self.use_repo_task()
                analyst.analyze(repo="r", prompt=PROMPT, client=self.client, order=ORDER,
                                budget=Budget(), proposals_root=self.proposals,
                                snapshot_root=self.snapshots, new_id=lambda: "prop1")
            else:
                analyst.analyze(proposal_id="prop1", answer=answer, client=self.client, order=ORDER,
                                budget=Budget(), proposals_root=self.proposals)
        return caught.exception

    def test_failed_attempts_are_saved_sanitized_in_state_history_and_the_exception(self):
        exc = self.fail()
        self.assertEqual(exc.proposal_id, "prop1")
        state = self.state()
        self.assertEqual(state["status"], "error")
        self.assertEqual([(a["kind"], a["outcome"]) for a in state["last_attempts"]],
                         [("primary", "model_error"), ("retry", "malformed"),
                          ("fallback", "model_error")])
        self.assertEqual(state["last_attempts"][0]["model_id"], PRIMARY)
        self.assertEqual(state["last_attempts"][2]["model_id"], FALLBACK)
        self.assertTrue(all(len(a["detail"]) <= 200 for a in state["last_attempts"]))
        self.assertIn("crusoe HTTP 503 No capacity at <project>/x", state["last_attempts"][0]["detail"])
        self.assertNotIn(str(analyst.PROJECT_ROOT), json.dumps(state))
        self.assertEqual(state["history"][-1]["attempts"], state["last_attempts"])
        self.assertIn("model_error: crusoe HTTP 503", state["history"][-1]["analysis_error"])
        self.assertEqual(exc.attempts, state["last_attempts"])
        self.assertIn("primary model_error: crusoe HTTP 503", exc.summary)
        self.assertIn("retry malformed", str(exc))

    def test_resume_from_error_reruns_without_duplicating_the_answer(self):
        self.start([proposal_reply()], autonomous=True)
        self.fail("use the second column")
        history = self.state()["history"]
        self.assertEqual([entry for entry in history if "answer" in entry],
                         [{"question": "(feedback on the proposal for pkg/csvload.py:load)",
                           "answer": "use the second column"}])
        rounds = self.state()["question_rounds"]
        result = self.cont([proposal_reply()], None, autonomous=True)
        self.assertEqual(result["status"], "proposal")
        state = self.state()
        self.assertEqual(len([e for e in state["history"] if "answer" in e]), 1)
        self.assertEqual(state["question_rounds"], rounds)
        self.assertEqual(state["last_attempts"], [])
        sent = self.user_text()
        self.assertEqual(sent.count("use the second column"), 1)
        self.assertNotIn("503", sent)  # infrastructure errors never reach the model

    def test_resume_after_a_failed_first_analysis_uses_the_stored_prompt(self):
        self.fail()
        result = self.cont([proposal_reply()], None, autonomous=True)
        self.assertEqual(result["status"], "proposal")
        self.assertIn(PROMPT, self.user_text())
        self.assertEqual([e for e in self.state()["history"] if "answer" in e], [])

    def test_resume_without_an_answer_is_only_allowed_from_error(self):
        self.start([proposal_reply()])
        with self.assertRaises(ValueError):
            self.cont([proposal_reply()], None)

    def test_an_explicit_answer_is_accepted_from_error(self):
        self.fail()
        result = self.cont([proposal_reply()], "retry please, any reasonable target")
        self.assertEqual(result["status"], "proposal")
        self.assertIn("feedback on the previous result: error", self.user_text())


class NotFeasibleTests(AnalystCase):
    def test_not_feasible_has_reason_and_suggestions(self):
        result = self.start([infeasible_reply()])
        self.assertEqual(result["status"], "not_feasible")
        self.assertEqual(result["reason"], "the loader is I/O bound")
        self.assertEqual(result["suggestions"],
                         ["batch the reads", "extract the parsing into a pure function"])
        self.assertIsNone(result["proposal"])
        self.assertIsNone(result["proposal_path"])
        self.assertFalse((self.proposals / "prop1" / "proposal.json").exists())
        self.assertEqual(self.state()["status"], "not_feasible")
        with self.assertRaisesRegex(analyst.ProposalError, "only a proposal"):
            analyst.approve("prop1", session_id="s1", proposals_root=self.proposals)


class ValidationRetryTests(AnalystCase):
    def test_invalid_proposal_is_retried_once_with_the_error(self):
        result = self.start([proposal_reply(target="pkg/nope.py:load"), proposal_reply()],
                            validation_errors=["target file pkg/nope.py not found"])
        self.assertEqual(result["status"], "proposal")
        self.assertEqual(len(self.client.calls), 2)
        self.assertIn("REJECTED", self.user_text())
        self.assertIn("target file pkg/nope.py not found", self.user_text())
        self.assertNotIn("REJECTED", self.client.calls[0][2][1]["content"])
        self.assertEqual(self.state()["history"],
                         [{"validation_error": "target file pkg/nope.py not found"}])

    def test_second_failure_becomes_needs_input(self):
        result = self.start([proposal_reply(), proposal_reply()],
                            validation_errors=["generator uses open", "generator still uses open"])
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(len(result["questions"]), 1)
        self.assertIn("generator still uses open", result["questions"][0])
        self.assertIsNone(result["proposal"])
        self.assertFalse((self.proposals / "prop1" / "proposal.json").exists())
        again = self.cont([proposal_reply()], "retry")
        self.assertEqual(again["status"], "proposal")
        self.assertIn("generator still uses open", self.user_text())
        self.assertEqual(self.state()["question_rounds"], 1)

    def test_retry_may_end_in_questions_or_not_feasible(self):
        result = self.start([proposal_reply(), infeasible_reply()], validation_errors=["bad"])
        self.assertEqual(result["status"], "not_feasible")


class MalformedAndBudgetTests(AnalystCase):
    def test_malformed_primary_then_retry_recovers(self):
        result = self.start(["I think the answer is yes", proposal_reply()])
        self.assertEqual(result["status"], "proposal")
        self.assertEqual(len(self.client.calls), 2)

    def test_ambiguous_replies_are_malformed_and_fail_closed(self):
        ambiguous = proposal_reply() + "\n" + infeasible_reply()
        nested = json.dumps({"wrapper": json.loads(infeasible_reply())})
        bad_type = proposal_reply(timing_size=True)
        for reply in (ambiguous, nested, bad_type, proposal_reply(include="everything"),
                      json.dumps({"status": "proposal", "target": "a.py:f"}),
                      json.dumps({"status": "maybe"}), json.dumps({"status": "questions"}),
                      '{"status": "not_feasible", "reason": "x",}'):
            with self.assertRaises(MalformedOutput, msg=reply):
                analyst.parse_analyst_reply(reply)

    def test_all_attempts_malformed_raises_and_keeps_state_for_retry(self):
        self.use_repo_task()
        self.client = FixtureModelClient({PRIMARY: ["nope", "still nope"],
                                          FALLBACK: [ModelError("down")]})
        with self.assertRaises(analyst.AnalysisFailed) as caught:
            analyst.analyze(repo="r", prompt=PROMPT, client=self.client, order=ORDER, budget=Budget(),
                            proposals_root=self.proposals, snapshot_root=self.snapshots,
                            new_id=lambda: "prop1")
        self.assertEqual(caught.exception.proposal_id, "prop1")
        self.assertEqual(self.state()["status"], "error")
        result = self.cont([proposal_reply()], "please try again")
        self.assertEqual(result["status"], "proposal")

    def test_budget_is_enforced_before_any_call(self):
        broke = Budget(max_cost_usd=0.0001, prices={PRIMARY: 100.0, FALLBACK: 100.0})
        with self.assertRaises(analyst.AnalysisFailed) as caught:
            self.start([proposal_reply()], budget=broke)
        self.assertIn("budget", str(caught.exception))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.state()["status"], "error")

    def test_parser_handles_fences_think_and_trims_soft_fields(self):
        fenced = "<think>maybe {\"status\": 1}</think>\nHere:\n```json\n" + proposal_reply() + "\n```"
        self.assertEqual(analyst.parse_analyst_reply(fenced)["target"], "pkg/csvload.py:load")
        many = proposal_reply(hints=[f"h{i}" for i in range(15)], description="d" * 5000)
        parsed = analyst.parse_analyst_reply(many)
        self.assertEqual((len(parsed["hints"]), len(parsed["description"])), (10, 2000))
        with self.assertRaises(MalformedOutput):
            analyst.parse_analyst_reply(proposal_reply(generator_source="x" * 40_000))

    def test_history_rendering_includes_errors_and_answers(self):
        messages = analyst.analyst_messages(PROMPT, {"files": [], "sources": {}}, [
            {"question": "which?", "answer": "csv"}, {"validation_error": "bad generator"},
            {"prepare_error": "baseline too slow"}])
        text = messages[1]["content"]
        for needle in ("Q: which?", "A: csv", "bad generator", "baseline too slow"):
            self.assertIn(needle, text)


def child_script(body):
    return [sys.executable, "-c", "import signal, sys, time\n" + body]


class ApproveTests(AnalystCase):
    def run_prepare(self, result=None, side_effect=None):
        return mock.patch.object(analyst, "_run_prepare", return_value=result, side_effect=side_effect)

    def test_success_runs_prepare_repo_and_records_the_session(self):
        self.start([proposal_reply()])
        output = {"session_id": "s1", "task_id": "repo-abc"}
        with self.run_prepare((0, json.dumps(output), "")) as run:
            result = analyst.approve("prop1", session_id="s1", proposals_root=self.proposals)
        self.assertEqual(result, {"status": "prepared", "session_id": "s1", "prepare": output})
        (command, timeout, grace), _ = run.call_args
        self.assertEqual(command, [sys.executable, "-m", "optimizer_core", "prepare-repo",
                                   "--session-id", "s1", "--proposal",
                                   str(self.proposals / "prop1" / "proposal.json")])
        self.assertEqual((timeout, grace), (900, 60))
        state = self.state()
        self.assertEqual((state["status"], state["session_id"]), ("prepared", "s1"))
        self.assertFalse((self.proposals / "prop1" / ".lock").exists())
        with self.assertRaisesRegex(analyst.ProposalError, "only a proposal"):
            analyst.approve("prop1", session_id="s2", proposals_root=self.proposals)

    def test_popen_gets_the_project_cwd_and_captured_output(self):
        child = mock.Mock(returncode=0)
        child.communicate.return_value = ("out", "err")
        with mock.patch.object(analyst.subprocess, "Popen", return_value=child) as popen:
            self.assertEqual(analyst._run_prepare(["x"], 5, 6), (0, "out", "err"))
        self.assertEqual(popen.call_args.args, (["x"],))
        self.assertEqual(popen.call_args.kwargs["cwd"], analyst.PROJECT_ROOT)
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
        child.communicate.assert_called_once_with(timeout=5)

    def test_prepare_failure_is_recorded_and_the_next_answer_sees_it(self):
        self.start([proposal_reply()])
        error = "ValueError: target output is not deterministic"
        with self.run_prepare((1, "", error + "\n")):
            result = analyst.approve("prop1", session_id="s1", proposals_root=self.proposals)
        self.assertEqual(result, {"status": "prepare_failed", "error": error})
        state = self.state()
        self.assertEqual(state["status"], "proposal")
        self.assertEqual(state["history"], [{"prepare_error": error}])
        self.assertIsNone(state["session_id"])
        again = self.cont([questions_reply("Can the target avoid dict ordering?")],
                          "it sorts keys already")
        self.assertEqual(again["status"], "questions")
        self.assertIn("target output is not deterministic", self.user_text())
        self.assertIn("it sorts keys already", self.user_text())

    def test_prepare_error_text_has_paths_stripped_and_is_cut_to_the_tail(self):
        self.start([proposal_reply()])
        home = str(Path.home())
        noisy = ("early-noise " * 400 + f"File {analyst.PROJECT_ROOT}/private/repos/x.py and "
                 f"{home}/secret/dir failed: baseline too slow")
        with self.run_prepare((1, "", noisy)):
            result = analyst.approve("prop1", session_id="s1", proposals_root=self.proposals)
        error = result["error"]
        self.assertLessEqual(len(error), 2000)
        self.assertTrue(error.endswith("baseline too slow"))
        self.assertIn("<project>/private/repos/x.py", error)
        self.assertNotIn(str(analyst.PROJECT_ROOT), error)
        self.assertNotIn(home, error)
        self.assertIn("~/secret/dir", error)
        self.assertEqual(self.state()["history"], [{"prepare_error": error}])

    def test_timeout_is_a_prepare_failure_pointing_at_cleanup(self):
        self.start([proposal_reply()])
        with self.run_prepare(side_effect=subprocess.TimeoutExpired("x", 900)):
            result = analyst.approve("prop1", session_id="s1", proposals_root=self.proposals)
        self.assertEqual(result["status"], "prepare_failed")
        self.assertIn("timed out", result["error"])
        self.assertIn("python -m optimizer_core cleanup", result["error"])

    def test_timeout_interrupts_a_slow_child_with_sigint_before_any_kill(self):
        marker = Path(self.tmp.name) / "interrupted"
        command = child_script(
            "def stop(*a):\n    open(sys.argv[1], 'w').write('sigint')\n    sys.exit(3)\n"
            "signal.signal(signal.SIGINT, stop)\nprint('ready', flush=True)\ntime.sleep(60)\n")
        command.append(str(marker))
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            analyst._run_prepare(command, 1.5, 20)
        self.assertEqual(marker.read_text(), "sigint")  # a cleanup handler got to run
        self.assertLess(time.monotonic() - started, 10)

    def test_a_child_that_ignores_interrupts_is_killed_only_at_the_end(self):
        command = child_script("signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                               "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n")
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            analyst._run_prepare(command, 1.0, 1.0)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.8)  # both grace periods were honored first
        self.assertLess(elapsed, 15)

    def test_non_json_prepare_output_is_kept_as_raw_text(self):
        self.start([proposal_reply()])
        with self.run_prepare((0, "prepared ok\n", "")):
            result = analyst.approve("prop1", session_id="s1", proposals_root=self.proposals)
        self.assertEqual(result["prepare"], {"raw": "prepared ok\n"})

    def test_concurrent_operations_on_a_proposal_are_refused(self):
        self.start([proposal_reply()])
        seen = []

        def during_prepare(command, timeout, grace):
            for attempt in (
                    lambda: analyst.approve("prop1", session_id="s2", proposals_root=self.proposals),
                    lambda: analyst.analyze(proposal_id="prop1", answer="x", client=FixtureModelClient({}),
                                            order=ORDER, budget=Budget(),
                                            proposals_root=self.proposals)):
                with self.assertRaisesRegex(analyst.ProposalError, "busy") as caught:
                    attempt()
                seen.append(str(caught.exception))
            return 0, "{}", ""
        with self.run_prepare(side_effect=during_prepare):
            result = analyst.approve("prop1", session_id="s1", proposals_root=self.proposals)
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(len(seen), 2)
        self.assertIn(".lock", seen[0])
        self.assertFalse((self.proposals / "prop1" / ".lock").exists())
        self.assertEqual(self.state()["session_id"], "s1")

    def test_lock_is_released_on_errors_and_a_stale_lock_is_reported(self):
        self.start([proposal_reply()])
        with self.run_prepare(side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                analyst.approve("prop1", session_id="s1", proposals_root=self.proposals)
        self.assertFalse((self.proposals / "prop1" / ".lock").exists())
        (self.proposals / "prop1" / ".lock").write_text("123")
        with self.assertRaisesRegex(analyst.ProposalError, "stale lock"):
            self.cont([questions_reply()], "answer")
        self.assertEqual(self.client.calls, [])

    def test_state_changed_during_prepare_is_refused_not_overwritten(self):
        self.start([proposal_reply()])
        path = self.proposals / "prop1" / "state.json"

        def tamper(command, timeout, grace):
            state = json.loads(path.read_text())
            state["status"] = "questions"
            path.write_text(json.dumps(state))
            return 0, "{}", ""
        with self.run_prepare(side_effect=tamper):
            with self.assertRaisesRegex(analyst.ProposalError, "changed to status questions"):
                analyst.approve("prop1", session_id="s1", proposals_root=self.proposals)
        self.assertEqual(self.state()["status"], "questions")
        self.assertIsNone(self.state()["session_id"])

    def test_ids_are_validated_and_unknown_proposals_rejected(self):
        for bad in ("../x", "a/b", "", "x" * 129, "a b"):
            with self.assertRaises(ValueError, msg=bad):
                analyst.approve("prop1", session_id=bad, proposals_root=self.proposals)
            with self.assertRaises(ValueError, msg=bad):
                analyst.approve(bad, session_id="s1", proposals_root=self.proposals)
        with self.assertRaisesRegex(analyst.ProposalError, "no proposal"):
            analyst.approve("missing", session_id="s1", proposals_root=self.proposals)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, value, name="c.json"):
        path = Path(self.tmp.name) / name
        path.write_text(value if type(value) is str else json.dumps(value))
        return path

    def good(self):
        return json.loads((ROOT / "nofreewins.config.json").read_text())

    def test_project_config_loads_with_todays_values(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NFW_CONFIG", None)
            config = nfw_config.load_config()
        self.assertEqual(config["models"]["analyst"], ("deepseek-ai/DeepSeek-V4.1-Flash", "openai/gpt-oss-120b"))
        self.assertEqual(config["models"]["verifier"], ("glm-5.3-flash", "deepseek-v4-flash"))
        self.assertEqual(config["run"], {"deadline_seconds": 1800, "max_rounds": 2, "max_cost_usd": 5,
                                         "max_tokens_per_call": 8192, "parallel": True})
        self.assertEqual(config["analysis"], {"max_cost_usd": 1, "max_question_rounds": 5,
                                              "max_tokens_per_call": 8192})
        self.assertEqual(config["repo"], {"include": "full", "timing_size": 40000,
                                          "correctness_sizes": [0, 1, 2, 3, 5, 8, 13, 50, 200, 1000]})
        self.assertEqual(config["auto"], {"max_prepare_attempts": 3, "max_runs": 2,
                                          "max_total_cost_usd": 10, "max_analyst_retries": 2})
        self.assertEqual(nfw_config.DEFAULT_CONFIG_PATH, ROOT / "nofreewins.config.json")

    def test_bad_values_are_rejected_with_a_clear_message(self):
        def mutate(section, key, value):
            config = self.good()
            config[section][key] = value
            return config
        bad = [
            (mutate("run", "deadline_seconds", 1801), "deadline_seconds"),
            (mutate("run", "deadline_seconds", 0), "deadline_seconds"),
            (mutate("run", "deadline_seconds", True), "deadline_seconds"),
            (mutate("run", "max_cost_usd", 26), "max_cost_usd"),
            (mutate("run", "max_cost_usd", -1), "max_cost_usd"),
            (mutate("run", "max_rounds", 0), "max_rounds"),
            (mutate("run", "max_rounds", 1.5), "max_rounds"),
            (mutate("run", "max_tokens_per_call", 255), "max_tokens_per_call"),
            (mutate("run", "max_tokens_per_call", 32769), "max_tokens_per_call"),
            (mutate("analysis", "max_cost_usd", 0), "analysis.max_cost_usd"),
            (mutate("analysis", "max_question_rounds", 0), "max_question_rounds"),
            (mutate("analysis", "max_tokens_per_call", "8192"), "max_tokens_per_call"),
            (mutate("auto", "max_prepare_attempts", 6), "auto.max_prepare_attempts"),
            (mutate("auto", "max_prepare_attempts", 0), "auto.max_prepare_attempts"),
            (mutate("auto", "max_analyst_retries", 5), "auto.max_analyst_retries"),
            (mutate("auto", "max_analyst_retries", -1), "auto.max_analyst_retries"),
            (mutate("auto", "max_runs", 4), "auto.max_runs"),
            (mutate("auto", "max_runs", True), "auto.max_runs"),
            (mutate("auto", "max_total_cost_usd", 25.5), "auto.max_total_cost_usd"),
            (mutate("auto", "max_total_cost_usd", 0), "auto.max_total_cost_usd"),
            ({k: v for k, v in self.good().items() if k != "auto"}, "auto must be an object"),
            (mutate("repo", "include", "everything"), "include"),
            (mutate("repo", "timing_size", 0), "timing_size"),
            (mutate("repo", "correctness_sizes", []), "correctness_sizes"),
            (mutate("repo", "correctness_sizes", [1, -1]), "correctness_sizes"),
            (mutate("repo", "correctness_sizes", [1, True]), "correctness_sizes"),
            (mutate("models", "planner", ["only-one"]), "models.planner"),
            (mutate("models", "planner", ["same", "SAME"]), "different"),
            (mutate("models", "planner", ["a", 3]), "models.planner"),
            ({**self.good(), "extra": 1}, "unknown top-level"),
            ({k: v for k, v in self.good().items() if k != "run"}, "run must be an object"),
            ([], "JSON object"),
        ]
        extra_key = self.good()
        extra_key["run"]["surprise"] = 1
        bad.append((extra_key, "unknown keys"))
        missing_role = self.good()
        del missing_role["models"]["analyst"]
        bad.append((missing_role, "missing: analyst"))
        for config, message in bad:
            with self.assertRaisesRegex(nfw_config.ConfigError, message, msg=str(config)[:80]):
                nfw_config.load_config(self.write(config))

    def test_unreadable_or_malformed_files_are_config_errors(self):
        with self.assertRaisesRegex(nfw_config.ConfigError, "cannot read"):
            nfw_config.load_config(Path(self.tmp.name) / "missing.json")
        for text in ("{", "[1,", '{"run": 1, "run": 2}', '{"run": NaN}', "\xff"):
            with self.assertRaisesRegex(nfw_config.ConfigError, "not valid JSON"):
                nfw_config.load_config(self.write(text))
        with self.assertRaisesRegex(nfw_config.ConfigError, "too large"):
            nfw_config.load_config(self.write(" " * (nfw_config.MAX_CONFIG_BYTES + 1)))

    def test_env_override_and_explicit_path_precedence(self):
        config = self.good()
        config["run"]["deadline_seconds"] = 600
        path = self.write(config)
        with mock.patch.dict(os.environ, {"NFW_CONFIG": str(path)}):
            self.assertEqual(nfw_config.load_config()["run"]["deadline_seconds"], 600)
            self.assertEqual(nfw_config.load_config(ROOT / "nofreewins.config.json")
                             ["run"]["deadline_seconds"], 1800)
        with mock.patch.dict(os.environ, {"NFW_CONFIG": str(Path(self.tmp.name) / "no.json")}):
            with self.assertRaises(nfw_config.ConfigError):
                nfw_config.load_config()


class LiveConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = json.loads((ROOT / "nofreewins.config.json").read_text())
        config["models"]["planner"] = ["gpt-oss-120b", "gemma-4-31b"]
        config["run"].update(deadline_seconds=600, max_rounds=1, max_cost_usd=3, max_tokens_per_call=2048)
        path = Path(self.tmp.name) / "c.json"
        path.write_text(json.dumps(config))
        patcher = mock.patch.dict(os.environ, {"NFW_CONFIG": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ids = LIVE_IDS + ["google/gemma-4-31b-it"]

    def test_lineup_and_run_defaults_come_from_the_config(self):
        self.assertEqual(live.LIVE_LINEUP["planner"], ("gpt-oss-120b", "gemma-4-31b"))
        self.assertEqual(set(live.LIVE_LINEUP), {"planner", "worker_a", "worker_b", "verifier"})
        orders = live.resolve_lineup(self.ids)
        self.assertEqual(orders["planner"].primary, "openai/gpt-oss-120b")
        with mock.patch.object(live, "make_preflight", lambda sid: (lambda: None)), \
                mock.patch.object(sessions, "load_session",
                                  lambda sid: {"schema_version": 1, "session_id": sid}):
            info = live.check_live("s1", available_ids=self.ids)
            self.assertEqual((info["deadline_seconds"], info["max_rounds"], info["max_tokens_per_call"],
                              info["budget"]["max_cost_usd"]), (600, 1, 2048, 3))
            self.assertEqual(info["models"]["planner"]["primary"], "openai/gpt-oss-120b")
            info = live.check_live("s1", available_ids=self.ids, deadline_seconds=300, max_rounds=4)
            self.assertEqual((info["deadline_seconds"], info["max_rounds"]), (300, 4))

    def test_importing_the_lineup_name_is_lazy_and_unknown_attributes_still_fail(self):
        with self.assertRaises(AttributeError):
            live.NOT_A_THING

    def test_run_flags_default_to_the_config_and_override_within_caps(self):
        seen = {}

        def capture(**kw):
            seen.update(kw)
            raise ValueError("stop here")
        with mock.patch.object(live, "build_live_run", capture), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["run", "--session-id", "s1", "--output",
                                       str(Path(self.tmp.name) / "o")]), 1)
            self.assertEqual((seen["deadline_seconds"], seen["max_rounds"], seen["max_cost_usd"],
                              seen["max_tokens"]), (600, 1, 3, 2048))
            cli.main(["run", "--session-id", "s1", "--deadline", "900", "--max-rounds", "3",
                      "--output", str(Path(self.tmp.name) / "o2")])
            self.assertEqual((seen["deadline_seconds"], seen["max_rounds"]), (900, 3))
            self.assertEqual(cli.main(["run", "--session-id", "s1", "--deadline", "1801"]), 2)

    def test_a_broken_config_is_a_one_line_runtime_error_for_run(self):
        Path(os.environ["NFW_CONFIG"]).write_text("{")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["run", "--session-id", "s1"]), 1)
        self.assertIn("not valid JSON", err.getvalue())
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)


class CliTests(unittest.TestCase):
    PROPOSAL = {"proposal_id": "prop1", "status": "proposal", "questions": [], "suggestions": [],
                "reason": None, "candidates": [], "target_source_excerpt": "",
                "proposal_path": "/x/proposal.json",
                "proposal": {"target": "pkg/csvload.py:load", "description": "Loads CSV.",
                             "hints": ["use split"], "risks": ["quotes"],
                             "generator_source": GENERATOR, "include": "package",
                             "sizes": {"timing_size": 20000, "correctness_sizes": [0, 1]}}}

    def invoke(self, *argv, analyze=None, approve=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli.CrusoeClient, "from_env", return_value=object()))
            if analyze is not None:
                stack.enter_context(mock.patch.object(analyst, "analyze", analyze))
            if approve is not None:
                stack.enter_context(mock.patch.object(analyst, "approve", approve))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_analyze_usage_errors_exit_2_without_calling_the_model(self):
        calls = []
        spy = lambda **kw: calls.append(kw)  # noqa: E731
        cases = (["analyze"], ["analyze", "--repo", "r"], ["analyze", "--prompt", "p"],
                 ["analyze", "--proposal-id", "p1"], ["analyze", "--answer", "a"],
                 ["analyze", "--repo", "r", "--prompt", "p", "--proposal-id", "p1"],
                 ["analyze", "--repo", "r", "--prompt", "p", "--answer", "a"],
                 ["analyze", "--proposal-id", "../x", "--answer", "a"],
                 ["analyze", "--proposal-id", "a" * 129, "--answer", "a"],
                 ["analyze", "--proposal-id", "p1", "--answer", " "],
                 ["analyze", "--repo", "r", "--prompt", " "],
                 ["analyze", "--repo", "r", "--prompt", "x" * 4001])
        for argv in cases:
            code, out, err = self.invoke(*argv, analyze=spy)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertEqual(len(err.strip().splitlines()), 1, argv)
        self.assertEqual(calls, [])

    def test_analyze_proposal_prints_json_and_a_human_summary(self):
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return self.PROPOSAL
        code, out, err = self.invoke("analyze", "--repo", "./r", "--prompt", PROMPT, analyze=fake)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.PROPOSAL)
        self.assertEqual((seen["repo"], seen["prompt"], seen["proposal_id"]), ("./r", PROMPT, None))
        for needle in ("pkg/csvload.py:load", "use split", "quotes", "approve --proposal-id prop1",
                       "--session-id", "--answer", "generator:"):
            self.assertIn(needle, err)

    def test_analyze_questions_not_feasible_and_continuation(self):
        questions = {**self.PROPOSAL, "status": "questions", "proposal": None,
                     "questions": ["Which loader?", "Which format?"], "candidates": ["a.py:f"]}
        code, out, err = self.invoke("analyze", "--proposal-id", "prop1", "--answer", "csv",
                                     analyze=lambda **kw: questions if kw["answer"] == "csv" else None)
        self.assertEqual(code, 0)
        self.assertIn("1. Which loader?", err)
        self.assertIn("2. Which format?", err)
        self.assertIn("a.py:f", err)
        declined = {**self.PROPOSAL, "status": "not_feasible", "proposal": None,
                    "reason": "I/O bound", "suggestions": ["batch reads"]}
        code, _, err = self.invoke("analyze", "--repo", "r", "--prompt", "p", analyze=lambda **kw: declined)
        self.assertEqual(code, 0)
        self.assertIn("Not feasible: I/O bound", err)
        self.assertIn("batch reads", err)

    def test_analyze_failures_are_one_line_errors_with_the_proposal_id(self):
        def failing(**kw):
            raise analyst.AnalysisFailed("analysis budget exhausted", proposal_id="prop9")
        code, out, err = self.invoke("analyze", "--repo", "r", "--prompt", "p", analyze=failing)
        self.assertEqual((code, out), (1, ""))
        report = json.loads(err)
        self.assertEqual(report["proposal_id"], "prop9")
        self.assertIn("budget exhausted", report["error"])
        self.assertNotIn("Traceback", err)

    def test_approve_usage_errors_exit_2(self):
        spy = mock.Mock()
        for argv in (["approve", "--proposal-id", "../x", "--session-id", "s1"],
                     ["approve", "--proposal-id", "p1", "--session-id", "a/b"],
                     ["approve", "--proposal-id", "p1", "--session-id", "x" * 129]):
            code, out, err = self.invoke(*argv, approve=spy)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertEqual(len(err.strip().splitlines()), 1)
        spy.assert_not_called()

    def test_approve_prepared_failed_and_missing(self):
        prepared = {"status": "prepared", "session_id": "s1", "prepare": {"task_id": "repo-1"}}
        code, out, err = self.invoke("approve", "--proposal-id", "p1", "--session-id", "s1",
                                     approve=lambda *a, **kw: prepared)
        self.assertEqual((code, json.loads(out)), (0, prepared))
        self.assertIn("run --session-id s1", err)
        failed = {"status": "prepare_failed", "error": "baseline too slow"}
        code, out, err = self.invoke("approve", "--proposal-id", "p1", "--session-id", "s1",
                                     approve=lambda *a, **kw: failed)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("baseline too slow", err)
        self.assertIn("--answer", err)

        def missing(*a, **kw):
            raise analyst.ProposalError("no proposal p1")
        code, out, err = self.invoke("approve", "--proposal-id", "p1", "--session-id", "s1",
                                     approve=missing)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no proposal p1", err)

    def test_approve_run_starts_the_live_run_with_config_defaults(self):
        prepared = {"status": "prepared", "session_id": "s1", "prepare": {}}
        with mock.patch.object(cli, "main_run", return_value=0) as run:
            code, out, _ = self.invoke("approve", "--proposal-id", "p1", "--session-id", "s1", "--run",
                                       approve=lambda *a, **kw: prepared)
        self.assertEqual((code, out), (0, ""))  # one JSON document on stdout: the run summary
        (args,), _ = run.call_args
        self.assertEqual((args.session_id, args.deadline, args.max_rounds, args.max_cost_usd,
                          args.max_tokens, args.inject_failures, args.check),
                         ("s1", None, None, None, None, False, False))
        with mock.patch.object(cli, "main_run", return_value=1) as run:
            code, _, _ = self.invoke("approve", "--proposal-id", "p1", "--session-id", "s1", "--run",
                                     approve=lambda *a, **kw: {"status": "prepare_failed", "error": "x"})
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_the_api_key_never_reaches_output(self):
        with mock.patch.dict(os.environ, {"CRUSOE_API_KEY": "sk-secret-123"}):
            code, out, err = self.invoke("analyze", "--repo", "r", "--prompt", "p",
                                         analyze=lambda **kw: self.PROPOSAL)
        self.assertNotIn("sk-secret-123", out + err)


class ImportIsolationTests(unittest.TestCase):
    def test_importing_the_analyst_loads_no_harness_backend_repo_task_or_daytona(self):
        code = ("import sys, agent_team.analyst, agent_team.config, agent_team.live, "
                "agent_team.__main__\n"
                "bad = [m for m in ('optimizer_core.harness', 'optimizer_core.backend',\n"
                "                   'optimizer_core.repo_task', 'daytona')\n"
                "       if m in sys.modules]\n"
                "print(','.join(bad))")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "")
