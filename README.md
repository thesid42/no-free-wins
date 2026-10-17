# No Free Wins

**Autonomous optimization. Every win verified.**

No Free Wins is a hackathon project for agents that optimize Python code and earn a performance claim through independent checks. A deterministic harness runs candidates in isolated Daytona sandboxes, compares outputs with a trusted reference, measures speedups, and produces auditable receipts.

The intended workflow is one approved contract followed by planning, two workers trying distinct strategies, screening, an independent verifier, and full acceptance or a bounded failure. **The deterministic harness is implemented; the live agent team has not been integrated into this repository yet.**

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
| Local tests | 43 tests passing |
| B: agent orchestration | Pending merge and integration from Anmol's work |
| Live Crusoe agents and independent model verifier | Pending integration and validation |
| Official autonomous run and demo | Pending |

### Development evidence

- A known fast reference passed fixed edge cases and 2,000 hidden cases. With five warmups per program and 30 timing pairs, it measured **3.69x median speedup**, with a **95% CI of 3.42–3.82x**. Its receipt audit passed.
- A small deterministic zoo rejected **4/4 planted attacks** and passed **4/4 honest controls** across three fixed public cases.
- Live checks exercised file/process/network restrictions, timer resistance, timeouts, and sandbox deletion. All owned test machines were confirmed deleted at the end of validation.

These are preparation results from a known reference and planted fixtures. They do not demonstrate agent-generated optimization, an evaluated model verifier, or a completed autonomous hackathon run. Correctness applies to the tested inputs. Local receipts and private evaluation artifacts are excluded from Git; their identifiers and the development receipt digest are recorded in [PREP.md](PREP.md#8-status-primary-sources-and-event-changes).

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

For a live development check, replace `python` below with your platform's virtual-environment Python executable:

```sh
python -m optimizer_core probe
python -m optimizer_core prepare --session-id my-dev-session
python -m optimizer_core evaluate --reference --session-id my-dev-session
```

These commands use Daytona credits. `--reference` measures the built-in known solution for calibration. For an offline synthetic receipt demonstration, use `python -m optimizer_core demo --output artifacts/my-synthetic-demo` with a fresh output directory.

## What's left

- [ ] **B / Anmol:** bring in `agent_team/`, orchestration tests, dependencies, and the preparation revision. Implement the planner, two distinct worker strategies, and independent verifier.
- [ ] **B:** validate live Crusoe model access, request/response handling, bounded retries and fallback, attempt/token/cost budgets, and terminal failure paths.
- [ ] **Joint:** replace B's fake harness with `optimizer_core.harness.evaluate_candidate`; connect candidate hashes, verifier evidence, receipts, and one official ledger writer.
- [ ] **A + joint:** validate the merged path, preserve both preparation baselines, freeze the evaluation configuration, and prepare a fresh official session before agents start.
- [ ] **Joint:** complete the autonomous run, audit its actual results, demonstrate labeled recovery after injected failures, and confirm cleanup.
- [ ] **Joint:** record the one-minute video, rehearse the three-minute demo, and submit the public repository, accessible demo, and team roster with preparation attribution.

Extra workloads, a larger zoo, the paired playbook study, long-duration claims, and prize-specific integrations are stretch work. The core deliverable is one verified optimization workflow with two agents, bounded recovery, and receipts.

## Team and repository layout

| Owner | Responsibility |
| --- | --- |
| [Sid / thesid42](https://github.com/thesid42) — A | Deterministic evaluation, Daytona isolation, timing, evidence, and audit |
| [Anmol / Anmol-tech](https://github.com/Anmol-tech) — B | Agent orchestration, live model adapters, verifier, recovery, and shared playbook |

`optimizer_core/` contains A's implementation; `tests/` contains its automated checks. [PREP.md](PREP.md) is the detailed working plan and integration contract. `pre_event_manifest.json` preserves the original preparation inventory. `.env.example` lists supported local settings. Credentials, private seeds, virtual environments, and generated artifacts are Git-ignored.

The deterministic foundation was prepared before the event. Git baseline `b5e1933` records its import; it is not a trusted timestamp of original authorship. Naming, repository setup, and this overview were added afterward. Event-built features will be recorded in PREP.md and distinguished from preparation in the submission.
