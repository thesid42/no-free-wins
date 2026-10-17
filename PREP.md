# No Free Wins — plan, setup and A/B handoff

Detailed working plan and integration contract. [README.md](README.md) is the project overview with current status and remaining work. Preparation baseline: Friday, October 16, 2026 (PDT). Naming and repository setup: October 17, 2026.

Autonomous optimization. Every win verified.

Public repository: [thesid42/no-free-wins](https://github.com/thesid42/no-free-wins). This document contains setup, the A/B interface, validation evidence, and attribution.

Goal: approve one contract, then let two agents optimize a workload, check their results, recover within budgets, and return an auditable receipt. Core target: at least 3x median speedup with a 95% bootstrap CI lower bound of at least 3x, matching outputs on the tested surface. This is not proof of universal equivalence or immunity to cheating.

## 1. Quick start — A's harness

Python 3.11+; Windows and macOS use native clients. Execution happens in hosted Linux on Daytona. No WSL required. Crusoe remains the inference provider for B's agents.

Copy .env.example to .env on a fresh clone; A's local .env already exists and is ignored by Git. Keep real keys only there. These names are supported:

    DAYTONA_API_KEY=<your key>
    DAYTONA_API_URL=https://app.daytona.io/api
    DAYTONA_TARGET=
    DAYTONA_SNAPSHOT=
    DAYTONA_MAX_SANDBOXES=100
    DAYTONA_MAX_COMPUTE_SECONDS=900
    DAYTONA_RUN_BUDGET_USD=5
    DAYTONA_HOURLY_USD=0.50
    CRUSOE_API_KEY=

Blank region/snapshot uses organization defaults. The 0.50 USD/hour rate is a conservative reservation for the allowed CPU/RAM/disk envelope, not a claim about your balance or final bill. Existing DAYTONA_CPU_HOURLY_USD is obsolete and ignored. Prices/credit balances remain separate from Crusoe tokens. Controller environment variables take precedence over .env; values are never shell-executed or printed by doctor.

Windows setup and commands:

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    .venv\Scripts\python.exe -m optimizer_core doctor
    .venv\Scripts\python.exe -m unittest discover -s tests -v
    .venv\Scripts\python.exe -m optimizer_core probe
    .venv\Scripts\python.exe -m optimizer_core prepare --session-id dev-20261016
    .venv\Scripts\python.exe -m optimizer_core evaluate --reference --session-id dev-20261016

The project venv and pinned SDK are already installed on A's Windows machine. On Mac use python3 for venv creation and .venv/bin/python for subsequent commands. B does not need A's repository or Daytona credentials to work independently today.

Other commands:

    python -m optimizer_core demo --output artifacts/new-synthetic-demo
    python -m optimizer_core evaluate --request artifacts/request.json
    python -m optimizer_core audit artifacts/evaluations/<receipt-id>/receipt.json --sha256 <receipt-digest>
    python -m optimizer_core cleanup
    python -m optimizer_core zoo

Use a fresh directory for a synthetic demo; it never claims real acceptance. Evaluate accepts JSON on stdin if --request is omitted. --reference is explicitly development calibration using a known solution, not an autonomous agent result. Prepare the session before workers start: it stores a private 128-bit random seed and publishes its hash plus generator/workload commitments under artifacts/commitments. Existing commitments are preserved; changed generator/size needs a new session ID. Use a fresh official session Saturday, never present development data as an untouched held-out result.

Artifacts and private seeds are Git-ignored. Raw timing proofs, responses, receipts and a host hash-chain ledger go under artifacts/. The cleanup command only touches sandbox IDs/names recorded by this project. Do not run cleanup against an active evaluation.

## 2. Ownership — independent today, merge at the venue tomorrow

| Owner | Environment | Work |
|---|---|---|
| A: Sid | Windows, native Python, Daytona Linux | Deterministic harness, protected supervisor, output schema/oracle/corpus, timing, independent zoo labels, receipts/audit |
| B: teammate | Mac, native Python, own repo | Orchestration, contract/planner/workers/verifier, distinct strategies, deadlines/budgets/recovery, shared playbook/regression flow |

A owns optimizer_core/ and tests for the harness/backend. B owns a separate agent_team/ package and tests/test_orchestration.py. No cross-imports today. B implements a fake harness with the JSON contract below. There is no need for a web service or a shared cloud filesystem. Tomorrow at 10:45AM, place the two packages alongside one another, preserve both pre-event baselines, select one controller and official ledger writer, and replace B's fake with A's evaluate_candidate function.

### B's standalone Friday assignment

1. Build a bounded state machine: plan -> two distinct worker orders -> candidates -> screen -> verifier -> full acceptance or terminal failure. Fixture responses are enough today.
2. Define evaluate_candidate(request) -> response against the plain JSON below; no import from A required. Fake results always have origin=synthetic and accepted=false. A successful simulation ends simulation_completed, never verified_done.
3. Add unique IDs, candidate source hashes, UTC timestamps, monotonic deadlines, heartbeats and automatic termination. One writer owns the official ledger; model explanations never choose acceptance.
4. Reserve attempts/tokens/cost before calls. Per model order: primary attempt, one bounded retry, one configured fallback; malformed-output repair consumes an attempt. No nested unbounded retry loops.
5. Test fallback success after two injected failures, persistent failure, deadline exhaustion, missing/duplicate candidate, absent second strategy, incomplete evidence and synthetic-origin rejection.
6. Feed only public evidence categories/dev reproducers back to workers; never hidden inputs, answers or private seeds. Stub playbook/regression interfaces. Add live Crusoe and evidence-backed revisions Saturday, attributing any Friday additions honestly.

Mac commands in B's own project:

    python3 --version
    python3 -m unittest discover -s tests -v

B brings agent_team/, their tests, dependency list, fixture requests/responses and pre-event revision/manifest. No WSL, A checkout, Daytona key or Friday integration meeting required.

### Integration contract v1

After the merge:

    from optimizer_core.harness import evaluate_candidate
    response = evaluate_candidate(request)

Request, illustrative placeholder source/hash:

    {
      "schema_version": 1,
      "request_id": "req-001",
      "session_id": "official-20261017",
      "task_id": "logs-parse-v1",
      "candidate": {
        "candidate_id": "worker-a-round-1",
        "source_utf8": "<Python source defining solve(lines)>",
        "sha256": "<SHA-256 of exact transmitted UTF-8 bytes>"
      },
      "mode": "screen",
      "remaining_wall_seconds": 900,
      "verifier": null
    }

No local file paths, shell commands, hidden inputs or caller-supplied timings. Preserve exact CRLF/LF source bytes when hashing. Mode is screen or acceptance. Acceptance requires verifier={"approved":true,"evidence_ids":["candidate-bound-verdict-id"]}, recorded by B's trusted controller for that same candidate, never copied from generated code. A recomputes the source hash, owns hidden cases/timing and enforces the fixed acceptance rule. B must prepare the matching session before starting worker calls.

Response shape:

    {
      "schema_version": 1,
      "request_id": "req-001",
      "candidate_id": "worker-a-round-1",
      "status": "screened",
      "origin": "synthetic",
      "accepted": false,
      "correctness_passed": true,
      "screen_median_speedup": 4.0,
      "acceptance": null,
      "receipt_id": null,
      "receipt_sha256": null,
      "evidence_ids": ["fixture-screen-001"],
      "feedback_public": []
    }

The 4.0 above is a synthetic illustration. Screen never accepts. Fake acceptance uses simulated_acceptance and accepted=false. Real verified_done requires status=accepted, origin=measured, accepted=true, correctness=true, receipt ID/digest, and full acceptance metrics (30 pairs, median >=3, CI lower >=3). A chooses origin based on its backend, not on the request. Other statuses: rejected, bar_not_met, budget_exhausted, infrastructure_error. Feedback contains code/summary/evidence IDs, not private counterexamples. Audit success alone means a recorded decision reproduces, not that a new optimization was measured.

## 3. Daytona isolation and timing

One disposable Daytona Linux machine per evaluation keeps baseline and candidate on the same hardware. Each trial gets a fresh unprivileged Python process and fresh scratch; the whole trial tree is removed afterward. The machine is deleted with confirmation before returning a measured acceptance. Fresh machines for every trial were rejected during development because hardware fingerprints differed across allocations.

The outer Daytona boundary protects the laptop. The inner supervisor protects timing/results from generated code. The default image executes SDK commands as a non-root user, so the trusted supervisor uses passwordless sudo; images without the required capability fail closed. Candidates run as UID/GID 65534, with no inherited groups, credentials or shell access to the controller. Protected files are root-owned; the API user's group may read proof bytes but cannot write them. Candidate code is never imported into the supervisor or run on either laptop.

Required controls implemented by the remote runner:

- Linux Landlock ABI >=3: runtime and current source readable, writes restricted to fresh scratch, no other trial/controller/private files readable. Source/runtime/proof paths are not candidate-writable.
- libseccomp: sockets, cross-process inspection/signaling, process/thread creation, namespace/mount changes, kernel interfaces and persistent IPC are denied. Contract v1 is single-process, single-thread standard-library Python. This makes scratch deletion plus a fresh process a complete reset of permitted candidate-writable state.
- Memory/file/descriptor/CPU limits; 64 MiB scratch and 1,024-file monitor; 1 MiB combined output cap; monotonic parent deadline and kill/reap. Unsupported controls fail closed. Candidate clocks never supply timings.
- Candidate receives only current input/batch. No keys, oracle, complete corpus/seeds, evaluator or ledger in execution. All hidden correctness comparison and acceptance math stay on the controller.
- Outer egress block-all requested too. Daytona's tier-dependent policy is not assumed sufficient: live candidate probes must prove socket denial. Probe also checks protected files/results, global writes, supervisor signaling, fork, no inherited keys, fake-clock resistance and timeout termination.

The protected parent times parsing, computation, full materialization, serialization and local output delivery. Provisioning, SDK transport/upload/download and cleanup are separate end-to-end/cost metrics; laptop HTTP latency is never CPU/process speedup. Proof bytes bind source, input, runner/driver hashes, environment, exit status, duration and isolation settings. Same-machine runtime/resource fingerprints must match every pair.

SDK 0.225.0 is pinned. The adapter has bounded SDK calls, TTL/auto-delete, explicit per-trial state removal, confirmed machine deletion, an owned-resource registry and reconciliation. Preflight can try at most three clean allocations when a host lacks required kernel controls, within combined time/cost caps; failed controls never permit candidate work. Candidate measurements are never retried automatically for a favorable score. Unconfirmed cleanup or malformed proof cannot produce acceptance. The default resource envelope is <=4 vCPU, <=8 GiB RAM, <=10 GiB disk; configured budgets reserve worst-case lifetime before creation. Account balance/tier and actual charges are not inferred from the key. Track credits separately from Crusoe inference.

## 4. Workload, protocol and receipts

Task logs-parse-v1: solve(lines) takes a list of strings. Valid lines exactly match:

    (DEBUG|INFO|WARN|ERROR) user=([A-Za-z0-9_-]{1,32}) latency=([0-9]{1,7})

Other lines are ignored. Leading zero latency digits are allowed. Output is {by_level: counts for all four levels, by_user: {user:{count,latency_sum}}}. Integer arithmetic; no floats, booleans, nulls, lazy/custom objects or duplicate JSON keys. Object key order is canonicalized by the host. Baseline parses then repeatedly scans per user; reference aggregates in one pass. No special-case input hardcoding or clock patches.

Input generator, baseline and reference are in optimizer_core/tasks.py. The default full workload is 100,000 lines, chosen before official runs after a 50,000-line development calibration was about 2.99x. This changes workload scale before freeze; the >=3x bar is unchanged. No development calibration is an official result.

Gates:

1. Validate request/source hash and narrow policy findings. A scanner is evidence, not proof of intent.
2. Match fixed edges plus 2,000 private generated cases: 1,800 small, 190 medium, 10 full-size. Batch small cases; every ID/result must appear exactly once. Corpus/generator/seed commitments are frozen before agents run.
3. Screen: 5 matched baseline/candidate pairs, ranking only. Verifier approval cannot mark done.
4. Acceptance: 5 warmup invocations per program, then 30 fresh matched pairs in precommitted randomized order. Check every timed output against oracle, including baseline. Full computation/materialization stays inside timing.
5. S_i=baseline_seconds_i/candidate_seconds_i. Median(S_i) >=3.0 and 95% percentile bootstrap lower bound >=3.0, using 10,000 resamples and a fixed recorded seed. No discarded outliers, partial-trial acceptance or favorable rerun selection.
6. Confirm cleanup, write complete receipt, audit it against its external digest, and return the decision. Honest-but-slow is bar_not_met, not cheating.

Budgets: caller wall deadline capped at 30 minutes; correctness stage <=3 minutes, screening <=5, acceptance including remote transport <=10. Each SDK operation is bounded and leaves cleanup time. The machine TTL is an additional hard bound. A late/failed stage is reported, never silently weakened. B owns inference caps: event project <=min($40,balance), official <=min($25,remaining), <=60 attempts/180k tokens; task <=12 attempts. These inference caps do not describe Daytona charges.

Receipt: source/baseline/task/generator/runtime hashes, private regeneration information, raw bound timing proofs, all required pairs/gates/settings, decision and deletion evidence. A's diagnostic ledger links receipt digests; B links the digest into the official message ledger. Audit verifies artifact bytes and recomputes the saved decision. A fresh execution is a new evaluate call and produces a new receipt; timings/verdict may drift. The hash chain is tamper-evident relative to a separately preserved final digest, not a digital signature.

Frozen evaluation and growing defenses are separate. Runtime catches may add validated regression fixtures or enable preimplemented detectors; they cannot change frozen thresholds/evaluator/policy. Any actual frozen-code edit or manual restart invalidates that official session. Replaying a planted fixture demonstrates regression coverage, not a new naturally observed attack.

Core zoo: four planted attacks (clock patch, hardcoding, early exit, lazy return), four honest controls (fast, small improvement, no-op, slower). Labels are A-owned; B tunes on separate fixtures. Target 4/4 caught, 0/4 honest accused, always report actual denominator and failure category. Runtime errors alone do not establish intent. Existing pre-event fixtures are disclosed; final evaluation labels/protocol freeze before scoring.

## 5. Evidence claims and scope

| Claim | Planned success | Falsifier |
|---|---|---|
| C1 | >=3x median and CI lower bound, matching tested outputs/full trials | Any mismatch, missing evidence or below-bar interval |
| C2 | Every done claim links an auditable receipt | Missing artifact or reproduced decision differs |
| C3 | Core zoo catch 4/4, honest false accusations 0/4 | Miss/false accusation; small pilot caveat remains |
| C4, stretch | Five untouched tasks, paired fixed-playbook-on/off with identical models/budgets/defenses; >=3 on-only wins, zero off-only | No positive separation; disabling playbook removing advantage supports effect |
| C5 | One approval, zero mid-run interventions, bounded termination | Manual edit/restart/response or a hanging session |
| C6 | Plan then two distinct workers; all handoffs/rejections logged | Missing plan/worker/strategy or unlogged handoff |
| C7 | Labeled injected primary+retry failures followed by real fallback recovery | No recovery, deadline overrun or manual assistance |
| C8, stretch | >=150 active minutes in one unmodified session, <=5-minute heartbeat gap | Shorter active work, unexplained gaps or intervention |

Core: one polished task, two agents, proven execution boundary, full acceptance, receipts and automatic recovery. No dashboard/third worker. C4, 24-case zoo, extra tasks, mutation testing and prize integration are stretch. Test growth alone is not C4. If running the five-task study, freeze tasks/references and learned playbook before the paired arms; reset memory between arms, keep defenses fixed, report all five outcomes and small-n limits. Never pad idle time to hit C8.

## 6. Event, rules and attribution

Saturday October 17, 9AM-10PM PDT, Shack15, 1 Ferry Building Suite 201, SF. Government photo ID; second-floor sign-in. Kickoff 10AM; hacking 10:45; lunch 1PM; submission 5PM; judging 5-6:45; dinner 6; finals 7-8; winners 8:15. Three-minute demo plus 1-2-minute Q&A. Round 1 weights: Technicality 40%, Live Demo 20%, Creativity/Originality 25%, Future Potential/AI Impact 15%; top six finals use equal weights. Team maximum four; we are two.

Supplied participant guide requires a public repo, accessible one-minute video/demo, all teammates on submission, and clear attribution of features/code built during the event. It does not expressly say the repo must be created Saturday or prohibit all preparation. Today's requested A foundation and B's preparation are pre-event work; do not present them as Saturday-built. Follow any stricter organizer clarification. A disclosure is not an organizer exemption or guarantee of eligibility. No Streamlit or dashboard as the main product; avoid all listed banned project categories. Keep venue Wi-Fi in private notes.

Preserve pre_event_manifest.json and both starting revisions; local hashes are an inventory, not a trusted timestamp. Record Saturday additions in this document's event-change section, and disclose preparation in submission/video. Do not hide/backdate history. New sessions/evaluation artifacts can show Saturday behavior while the submission/video explicitly distinguishes pre-event instrumentation from event-built agent integrations and features.

Crusoe guide offers $50 API credits: one org with teammate-specific tokens recommended. [Signup](https://console.crusoecloud.com/request-foundry), [credit claim](https://forms.gle/JdEvMGSb4TURH1Tq8), [official resources](https://github.com/crusoecloud/crusoe-developer-hub/tree/main/examples/hackathons/crusoe-cv-recursive-agents-hackathon). Actual key expiry/balance must be inspected; Oct 18 expiry was not stated in the supplied guide.

Model shortlist: accessible exact Nemotron Super worker ID; one tested Nano or DeepSeek fallback; GLM verifier. Check authenticated /v1/models and supported request parameters before freezing. No assumed JSON/reasoning-trace support. Kimi K2.6 is excluded based on the current Crusoe deprecation notice. [Catalog](https://docs.cloud.crusoe.ai/serverless-inference/index.html). Cache improvements are measured, not assumed from a vendor maximum.

Prizes: $14k/$8k/$3k plus You.com credits $1,000/$500/$250; Nemotron RTX 5090; NemoClaw/OpenShell $100 Brev/person. Daytona usage alone does not establish the NemoClaw/OpenShell prize integration.

## 7. Saturday schedule and demo

| PDT time | A | B / joint |
|---|---|---|
| 10:45-11:15AM | Preserve baselines, combine packages, connect JSON harness | Live adapter wiring; list actual event-built changes |
| 11:15AM-12:30PM | Task-specific checks, independent zoo, calibration/probes | Live Crusoe agents/verifier, strategy race and replanning |
| 12:30-1:15PM | Dry run; actual seed/generator/eval/model/budget freeze at 1:15 | Fix before freeze; no final-label tuning |
| 1:30 target, 2:00 latest | Launch bounded official session after one contract approval | Automatic fixed queue; no manual crash fixes |
| 3:15-3:30 | Rehearse completed receipts | Actual scoreboard and labeled fallbacks |
| 3:30-4:00 | Record video, automatic benchmark media pause | Planned idle excluded from active-duration claim |
| 4:00-4:30 | Remaining precommitted work only; hard stop 4:30 | Termination/deletion and final records |
| 4:30-4:45 | Audit/reveal after all arms finish, final report, submit | Check public access/video/team roster |

1:30-4:30 is 180 elapsed minutes; media pause leaves at most 150 active. A 2PM start leaves at most 120 active, so C8 cannot pass. Finish when work finishes. If blocked at launch deadline, report development evidence honestly; never bypass isolation or acceptance. A changed frozen implementation requires a new session/freeze/approval, not an unlogged crash fix.

Three-minute demo: goal/contract -> labeled rejection/regression -> actual two-worker conversation -> labeled recovery drill -> judge-selected completed receipt audit -> actual zoo/results/limitations. Audit replay is not fresh unseen optimization. Recorded excerpts show session/time. A planted case is never described as an independently generated worker attempt; an injected fault is never a real outage. No pre-filled winning numbers. New optimization/fresh execution runs outside the 35-second presentation slot.

One-minute video at 3:30: goal 6s; labeled rejection/regression 12s; two workers/receipt 14s; recovery 10s; actual report 10s; close 8s. If active, say in progress as of that time and use only completed evidence. Final report may contain later results. No 2.5-hour claim before it happens.

## 8. Status, primary sources and event changes

Implemented A: .env loader, strict JSON/canonical comparison, fixed acceptance math, seed commitments, workload/oracle/generator/zoo fixtures, real Daytona SDK adapter, protected Linux supervisor, lifecycle registry/cleanup, B-compatible harness, raw-proof receipts and audit, CLI. Candidate code never executes on the controller. B's orchestration/agent code is not implemented here.

Live checks have confirmed candidate isolation/timer resistance/timeout and machine deletion. A three-pair 50k-line reference calibration was about 2.99x; it is screening evidence, not acceptance. Full protocol status and final test results are recorded below after validation. A's environment contains a configured key; its value is never included in documentation, output or manifests.

Primary sources checked October 16:

- [Daytona isolation](https://www.daytona.io/docs/en/isolation/), [network policy](https://www.daytona.io/docs/en/network-limits/), [persistence](https://www.daytona.io/docs/en/persistence/), [SDK](https://www.daytona.io/docs/en/python-sdk/sync/daytona/), [pricing](https://www.daytona.io/pricing).
- [Event overview](https://events.cerebralvalley.ai/e/crusoe-recursive-agents-hackathon), plus the participant guide supplied in this chat for detailed schedule/rules.
- [METR reward hacking](https://metr.org/blog/2025-06-05-recent-reward-hacking/): over-43x comparison was across benchmark suites, not a controlled causal estimate of scorer visibility. Difficulty/scaffolding also differed.
- [CoT monitoring](https://arxiv.org/abs/2503.11926): monitoring evidence does not guarantee detection on our models/tasks; do not claim traces not exposed by the provider.
- [AIDE2](https://arxiv.org/abs/2609.26457): held-out improvement inspiration; published 55%-32% reward-hacking result is not our result.

Validation completed October 16:

- 43 automated tests pass; installed dependencies pass pip check.
- Live isolation/timer/timeout probes passed; all owned test machines are confirmed deleted.
- Full known-reference development run passed fixed edge cases plus 2,000 hidden cases, five warmups per program, and all 30 timing pairs: median 3.69x, 95% CI 3.42-3.82x. Receipt audit passed. This is a known-reference calibration, not an agent-generated or official hackathon result.
- Development deterministic zoo: four planted attacks rejected, four honest controls passed. Surface: three fixed public cases; no model verifier evaluated. This is not the final held-out zoo measurement.
- Receipt: artifacts/evaluations/aa38eb5656d54e83aa7f18cb484db1dc/receipt.json
- Receipt SHA-256: 2ead31665fac3297ba5242113d1de75c4c14a2f82848178796701fdc928228b9
- Zoo report: artifacts/zoo/c72e9f473088438891a435efe7139ac8/report.json

B's live agents, independent model verifier, full autonomous team run and any Version N+1 study remain B/integration work.

Repository setup on October 17: preserved the existing preparation files in baseline commit b5e1933, then named the project No Free Wins in PREP.md and pyproject.toml. The commit records when the files entered Git; the prior-work attribution is based on the disclosed preparation history. pre_event_manifest.json remains the original preparation inventory; its PREP.md and pyproject.toml hashes describe the baseline before these naming edits.

Documentation update on October 17: added README.md with a public project overview, validated preparation status, Windows/macOS setup, team ownership, and a remaining-work checklist. PREP.md remains the detailed plan and interface. This documentation update adds no agent/runtime functionality and does not change the original preparation manifest.

Event feature changes after Saturday's start (October 17):

- B's macOS controller setup: project venv on Homebrew Python 3.12 (the python.org 3.11 build lacked a CA bundle, so Daytona TLS failed before any sandbox was created). Live probe passed with confirmed deletion; a development reference calibration (session anmol-dev-1) measured median 3.50x, 95% CI 3.48-3.53x, and the receipt audit passed. This is a known-reference calibration, not an agent or official result.
- agent_team/ (event-built, B): contract v1 request/response checks and verified_done rule (contract.py); synthetic FakeHarness that can never claim acceptance (fake_harness.py); fixture model client (models.py); reserve-before-call attempt/token/cost budget (budget.py); flat primary/retry/fallback model orders with per-attempt deadline hook (calls.py); planner/worker/verifier prompts and parsers with public-only feedback (roles.py); playbook/regression stubs (playbook.py); bounded orchestrator state machine writing the official ledger via optimizer_core.ledger (orchestrator.py); `python -m agent_team simulate` synthetic CLI with optional labeled fault injection (__main__.py). Tests: tests/test_agent_foundations.py and tests/test_orchestration.py.
- agent_team imports only optimizer_core.canonical and optimizer_core.ledger; the harness is injected. Tests import optimizer_core.harness only to check that agent_team requests are accepted by A's real Harness with a synthetic backend.
- Real-harness runs keep the default time floors (screen 240 s, acceptance 840 s) and need a deadline near the 1,800 s cap; the simulate CLI lowers the floors only for the fake harness.
- agent_team/crusoe.py (event-built, B): stdlib Crusoe ModelClient for https://api.inference.crusoecloud.com/v1 — one request per attempt (retry/fallback stay in calls.py), every transport/HTTP/JSON/empty-content failure mapped to ModelError, redirects refused so the key cannot follow a 3xx, hard wall-clock and 8 MiB body bound, key redacted from errors; conservative per-model pricing (output rate per 1k total tokens) and case-insensitive model-ID resolution against the live listing; `python -m agent_team models [--resolve NAME]`. Tests: tests/test_crusoe.py (fake transport plus loopback-only servers).
- Live listing on October 17 showed 13 models. Chosen live lineup (IDs from /v1/models): planner and workers nvidia/NVIDIA-Nemotron-3-Super-120B-A12B with fallback nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B; verifier zai-org/GLM-5.3-Flash with fallback deepseek-ai/Deepseek-V4-Flash. Conservative output-rate prices per 1M tokens: $2.40, $0.20, $0.50, $0.28.
- Not yet done: wiring the live lineup and the real evaluate_candidate into a live run, official session.
