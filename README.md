# No Free Wins

**Autonomous optimization. Every win verified.**

No Free Wins is a hackathon project for agents that optimize Python code and earn a performance claim through independent checks. A deterministic harness runs candidates in isolated Daytona sandboxes, compares outputs with a trusted reference, measures speedups, and produces auditable receipts.

The workflow is one approved contract followed by planning, two workers trying distinct strategies, screening, an independent verifier, and full acceptance or a bounded failure. **The integrated live development workflow has passed with agent-generated code: 3.75x median speedup, 95% CI 3.52–3.81x. The official event session remains pending.**

The full plan, A/B interface, execution model, event schedule, and attribution are in **[PREP.md](PREP.md)**.

## Current status

Status as of October 17, 2026:

| Area | Status |
| --- | --- |
| A: deterministic harness | Implemented and validated |
| Daytona execution | Live isolation, timing, timeout, and deletion checks passed |
| Workload | `logs-parse-v1`: strict parsing and aggregation with a trusted oracle |
| Acceptance | Correct outputs, 30 timing pairs, at least 3x median speedup and a 95% bootstrap CI lower bound of at least 3x |
| Evidence | Raw execution proofs, artifact hashes, receipts, audit, and a hash-chain ledger |
| Local tests | 411 Python tests and 84 UI bridge tests passing (repo input, analyst, fault drill, replay API) |
| B: agent orchestration | Event-built `agent_team/` state machine, retries/fallback, budgets, official ledger, and live `run` CLI |
| Live Crusoe agents and independent model verifier | Validated with real Nemotron workers, GLM verifier, and measured Daytona acceptance |
| Repo input | Event-built development feature: `repo-dev-1` on `examples/textstats` reached `verified_done` at 8.03× (95% CI 7.76–8.21×, receipt audited); not part of the official result |
| Shared playbook / regression growth | Placeholder interfaces; learning flow remains pending |
| Sidecar UI | Standalone local skeleton in `ui/`; scripted chat and pipeline scenes, with backend integration deliberately deferred |
| Official autonomous run and demo | Official session `official-20261017-3`: `verified_done`, median 3.54x, 95% CI 3.49–3.62x, receipt audit and ledger verified (sessions 1–2 failed on Daytona latency; all disclosed in PREP.md). Demo pending |

### Development evidence

- A known fast reference passed fixed edge cases and 2,000 hidden cases. With five warmups per program and 30 timing pairs, it measured **3.69x median speedup**, with a **95% CI of 3.42–3.82x**. Its receipt audit passed.
- A small deterministic zoo rejected **4/4 planted attacks** and passed **4/4 honest controls** across three fixed public cases.
- Live checks exercised file/process/network restrictions, timer resistance, timeouts, and sandbox deletion. All owned test machines were confirmed deleted at the end of validation.
- First integrated live development run (`dev-live-a-20261017-1`): two rounds, four generated candidates, no acceptance. One candidate passed correctness but screened at 2.81x; three failed correctness. The run stopped at its planned limit, its ledger verified, and all four evaluation machines were confirmed deleted. Inference usage: 11,961 tokens, about $0.0287 at the recorded conservative rates.
- Fresh live development run after public-contract clarification (`dev-live-a-20261017-2`): both workers produced correct candidates; the GLM verifier approved the selected candidate. Full acceptance passed 2,000 hidden cases, five warmups per program and 30 timing pairs: **3.75x median, 95% CI 3.52–3.81x**. Receipt audit and ledger verification passed; all three evaluation machines were confirmed deleted. The run finished in 8m 42s with four inference calls, 14,657 tokens and about $0.0249 estimated inference cost. Daytona costs are separate.

The known-reference and zoo numbers are preparation evidence. Both live development runs are retained, including the failed first run. The public task prompt was clarified before the second session; the oracle, hidden generator, workload scale and acceptance rules were unchanged. This is a development result, not an official event-session, shared-learning or live-fallback result. Correctness applies to the tested inputs. Local receipts and private evaluation artifacts are excluded from Git; identifiers and receipt digests are recorded in [PREP.md](PREP.md#8-status-primary-sources-and-event-changes).

## How it works

1. Prepare a session and commit to the private evaluation seed before workers start.
2. Submit candidate Python source and its exact UTF-8 SHA-256 through the JSON harness interface.
3. Run correctness checks and screening in a disposable Daytona machine, using a fresh restricted process and scratch directory for each trial.
4. Require candidate-bound verifier evidence before full acceptance.
5. Materialize outputs inside the measured interval, compare against the oracle, and evaluate all 30 timing pairs.
6. Confirm sandbox deletion, seal the evidence, and audit the receipt before returning acceptance.

Candidate code never executes on the controller. Synthetic demonstrations always return `accepted=false`. The detailed security model and its limits are in [PREP.md](PREP.md#3-daytona-isolation-and-timing).

## Quick start

Use Python 3.11 or newer. Windows and macOS run native clients; Daytona hosts the Linux execution environment. No WSL is needed.

```sh
git clone https://github.com/thesid42/no-free-wins.git
cd no-free-wins
```

Windows PowerShell:

```powershell
python -m venv .venv
Copy-Item .env.example .env
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m optimizer_core doctor
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

macOS:

```sh
python3 -m venv .venv
cp .env.example .env
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m optimizer_core doctor
.venv/bin/python -m unittest discover -s tests -v
```

On a fresh clone, copy the example once, then add your own `DAYTONA_API_KEY` to `.env`. Keep keys out of Git. `doctor` checks local configuration without making live API calls; the unit tests do not allocate sandboxes.

For live commands, replace `python` below with your platform's virtual-environment Python executable:

```sh
python -m optimizer_core probe
python -m optimizer_core prepare --session-id my-dev-session
python -m optimizer_core evaluate --reference --session-id my-dev-session
```

These commands use Daytona credits. `--reference` measures the built-in known solution for calibration. For an offline synthetic receipt demonstration, use `python -m optimizer_core demo --output artifacts/my-synthetic-demo` with a fresh output directory.

To run the integrated agents, also set `CRUSOE_API_KEY` in your local `.env`. Prepare a fresh development session, validate setup, then start a bounded live run:

```sh
python -m optimizer_core prepare --session-id my-agent-dev
python -m agent_team run --session-id my-agent-dev --check
python -m agent_team run --session-id my-agent-dev --max-cost-usd 1 --max-rounds 2 --deadline 1800
```

`--check` lists models and checks configuration; it makes no inference calls or sandbox allocations. `run` spends Crusoe and Daytona credits, labels itself as development, and returns `verified_done` only after measured acceptance. The inference cap and Daytona budget are separate. Failed requests with unknown usage retain their token/cost reservation; reported usage is recorded even when the final answer is unusable. Late model replies cannot become verdicts, late harness returns cannot become `verified_done`, and cleanup may still finish after the work deadline.

## Sidecar UI preview

The selected interface has chat on the left and a pipeline canvas on the right. It includes seven stage views (start, analysis, workers, verification, timing race, cheating zoo, result), a line graph with animated handoffs, recorded fallback recovery, candidate source excerpts, replay/pause, and code/proof downloads. ASCII mode has been removed. Chat stays available with one milestone per major stage and contextual questions. Narrow screens stack the pipeline above the conversation; light/dark themes and reduced motion follow system preferences.

Use Node.js 22 or newer on Windows or macOS. No npm dependencies or API keys are needed:

```sh
cd ui
npm run dev
```

Open `http://127.0.0.1:5173`. Run `npm run check` for JavaScript syntax checks.

**Replay integration (event-built, development).** When `npm run dev` is started from this repository, `ui/server.mjs` also serves a local-only API (127.0.0.1, same-origin, read-only) over this machine's `artifacts/runs/` ledgers: a recorded-runs list, ledger events streamed into the seven scenes, results taken from audited receipts, downloads of the optimized code, original code and proof, and chat answers templated from the run's real data (no model calls). A replay is a recording, not a new run. Launching runs, analyst questions and approval from the UI are not connected yet; the link box only explains this. Without the API the page falls back to the original scripted demo. `npm test` runs the bridge tests.

The race can replay the actual 30 measured pairs from an audited accepted receipt, showing the stored durations and matched-output status. It never invents progress when only an aggregate result is available. The zoo is a separate recorded development pilot, not an acceptance stage of the selected run. Results include a bounded source diff and an expandable proof panel. Replaying these scenes starts no model calls or sandbox jobs.

## Optimize your own repo (development)

This is an event-built development feature, added after the freeze. It is not part of the official result, which remains `official-20261017-3` on frozen commit `b99fc1f`. The spec-file path was validated live on `examples/textstats` (`repo-dev-1`, 8.03×); the analyst path below is pending live validation.

**Recommended: describe what you want.** No file is needed in the target repo. An analyst agent reads the repo and your prompt, picks the function, writes an input generator, and proposes optimizations. If something is unclear it asks; if the request can't be optimized safely it says why and suggests alternatives. Nothing runs until you approve.

```sh
python -m agent_team analyze --repo PATH|https://github.com/owner/name[@ref] --prompt "make the CSV loader faster"
python -m agent_team analyze --proposal-id P --answer "use load_rows in io/csv.py"
python -m agent_team approve --proposal-id P --session-id my-repo-dev --run
```

Models, budgets, run limits and default sizes come from the global `nofreewins.config.json` in this repository; command-line flags override them within the hard caps.

**Alternative: a spec file.** Point No Free Wins at a repo that contains a `nofreewins.json` naming one target function plus an input generator. The agents then try to write a faster `solve(data)` that returns exactly what the target returns.

```sh
python -m optimizer_core prepare-repo --session-id my-repo-dev --repo PATH|https://github.com/owner/name[@ref] [--include package|full]
python -m agent_team run --session-id my-repo-dev --check
python -m agent_team run --session-id my-repo-dev --deadline 1800
```

The repo needs a `nofreewins.json` at its root. `examples/textstats/` is a tiny repo with a slow `top_words(data)`, where `data` is `{"lines": [...], "k": 10}` and the result is the top `k` `[word, count]` pairs. It ships with a generator and this spec:

```json
{
  "target": "textstats/core.py:top_words",
  "generator": "textstats/gen.py:generate_input",
  "include": "package",
  "timing_size": 40000,
  "correctness_sizes": [0, 1, 2, 3, 5, 8, 13, 50, 200, 1000],
  "description": "Return the k most frequent words ..."
}
```

- `target`: `path:function`. The function takes one positional argument. Input and output must be JSON-safe. Output may contain only ints, strings, lists and objects: no floats, booleans or null.
- `generator`: `path:function`, called as `(seed, size)`. It must be deterministic.
- `include`: `package` (the target's top-level package) or `full` (every `.py` file, excluding tests, `.git`, virtualenvs and build directories).
- `timing_size` sets the size of the timing workloads (each generated input must stay under 1 MiB; 40,000 words is about 300 KB and gives a baseline of roughly 0.12–0.15 s). `correctness_sizes` is optional and cycles over the 2,000 hidden cases. `description` describes the target.
- Return lists, not tuples: the sandbox driver rejects tuples and non-string dict keys. Modules must be importable from the repo root (a `src/` layout is not supported yet).

How it is handled:

- Repo code (the target and the generator) never runs on the laptop. It is bundled (zlib + base64) with a small in-memory importer and runs only inside the Daytona sandbox, under the same supervisor and limits as candidates. The frozen sandbox runner is unchanged.
- `prepare-repo` runs the generator in the sandbox to create 2,000 hidden correctness inputs and 40 timing workloads (5 warmup, 5 screen, 30 acceptance). It runs the original function in the sandbox to record the expected outputs.
- Everything is sealed under `private/sessions/<S>.repo/` (mode 0600), with digests in the session file. The GitHub commit SHA and the bundle digest are recorded.
- The original function is both the baseline and the oracle. Acceptance is unchanged: 30 pairs, median at least 3x and a 95% CI lower bound of at least 3x.
- Runs on user repos are labeled "development live run on user repo; not an official session" in the ledger.

Limits:

- Standard-library Python only; nothing is installed.
- Bundled source (prelude plus bundle) is at most 192 KiB, so candidates fit under the frozen 256 KiB sandbox source limit.
- Each generated input and each trial output is at most 1 MiB.
- Public GitHub only, shallow clone, at most 50 MiB.
- Short workloads give noisier timing.

The harness task abstraction behind this was added after the freeze. The `logs-parse-v1` behavior is unchanged and covered by the existing tests. No result, test count or live run is claimed for repo input yet: pending validation.

## What's left

- [x] **B / Anmol:** `agent_team/` orchestration (planner → two distinct workers → screen → verifier → acceptance), bounded primary/retry/fallback, reserve-before-call budgets, deadlines, hash-chained official ledger, and orchestration tests. Built on October 17 with fixture models and a fake harness.
- [x] **B:** live Crusoe `ModelClient` adapter with per-model prices (`agent_team/crusoe.py`, `python -m agent_team models`).
- [x] **B + A:** validate live Crusoe generation and independent verification through measured Daytona acceptance. Both successful and unsuccessful development runs are recorded; retry/fallback and failure paths also have offline regression coverage.
- [x] **Joint:** wire `optimizer_core.harness.evaluate_candidate` through `agent_team/live.py`, with candidate hashes, verifier evidence, receipts, and one official ledger writer.
- [ ] **B / joint:** implement the shared playbook and validated regression flow; the current classes are placeholders.
- [ ] **UI / deferred:** connect source ingestion, live chat, pipeline events, actual artifact delivery, and failure/cancellation states when integration is requested. The Sidecar skeleton currently uses local demo state only.
- [ ] **A + joint:** validate the merged path, preserve both preparation baselines, freeze the evaluation configuration, and prepare a fresh official session before agents start.
- [x] **Joint:** complete a live autonomous development run, audit its actual result, verify the official message ledger, and confirm cleanup.
- [ ] **Joint:** complete the official session and a labeled live recovery drill after injected failures. The recorded successful development run used primary model calls throughout.
- [ ] **Joint:** validate repo input live on `examples/textstats` and a GitHub URL (development feature, not part of the official result).
- [ ] **Joint:** record the one-minute video, rehearse the three-minute demo, and submit the public repository, accessible demo, and team roster with preparation attribution.

Extra workloads, a larger zoo, the paired playbook study, long-duration claims, and prize-specific integrations are stretch work. The core deliverable is one verified optimization workflow with two agents, bounded recovery, and receipts.

The cheating zoo remains a separate defense test suite. Dedicated honeytraps are outside the agreed scope. The current live CLI caps a run at 30 minutes; it does not establish the optional 150-active-minute claim.

## Team and repository layout

| Owner | Responsibility |
| --- | --- |
| [Sid / thesid42](https://github.com/thesid42) — A | Deterministic evaluation, Daytona isolation, timing, evidence, and audit |
| [Anmol / Anmol-tech](https://github.com/Anmol-tech) — B | Agent orchestration, live model adapters, verifier, recovery, and shared playbook |

`optimizer_core/` contains A's implementation; `agent_team/` contains B's event-built orchestration; `tests/` contains automated checks for both. [PREP.md](PREP.md) is the detailed working plan and integration contract. `pre_event_manifest.json` preserves the original preparation inventory. `.env.example` lists supported local settings. Credentials, private seeds, virtual environments, and generated artifacts are Git-ignored.

The deterministic foundation was prepared before the event. Git baseline `b5e1933` records its import; it is not a trusted timestamp of original authorship. Naming, repository setup, and this overview were added afterward. Event-built features will be recorded in PREP.md and distinguished from preparation in the submission.
