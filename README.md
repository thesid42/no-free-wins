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
| Local tests | 239 tests passing, including late-response and failed-call usage regressions |
| B: agent orchestration | Event-built `agent_team/` state machine, retries/fallback, budgets, official ledger, and live `run` CLI |
| Live Crusoe agents and independent model verifier | Validated with real Nemotron workers, GLM verifier, and measured Daytona acceptance |
| Shared playbook / regression growth | Placeholder interfaces; learning flow remains pending |
| Official autonomous run and demo | Pending |

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

## What's left

- [x] **B / Anmol:** `agent_team/` orchestration (planner → two distinct workers → screen → verifier → acceptance), bounded primary/retry/fallback, reserve-before-call budgets, deadlines, hash-chained official ledger, and orchestration tests. Built on October 17 with fixture models and a fake harness.
- [x] **B:** live Crusoe `ModelClient` adapter with per-model prices (`agent_team/crusoe.py`, `python -m agent_team models`).
- [x] **B + A:** validate live Crusoe generation and independent verification through measured Daytona acceptance. Both successful and unsuccessful development runs are recorded; retry/fallback and failure paths also have offline regression coverage.
- [x] **Joint:** wire `optimizer_core.harness.evaluate_candidate` through `agent_team/live.py`, with candidate hashes, verifier evidence, receipts, and one official ledger writer.
- [ ] **B / joint:** implement the shared playbook and validated regression flow; the current classes are placeholders.
- [ ] **A + joint:** validate the merged path, preserve both preparation baselines, freeze the evaluation configuration, and prepare a fresh official session before agents start.
- [x] **Joint:** complete a live autonomous development run, audit its actual result, verify the official message ledger, and confirm cleanup.
- [ ] **Joint:** complete the official session and a labeled live recovery drill after injected failures. The recorded successful development run used primary model calls throughout.
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
