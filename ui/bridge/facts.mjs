// Derive public run facts and the VerifiedResult from raw ledger events plus audit output.
// Pure functions: inputs come from inspect_run.py (hash-chain verified) or the ledger reader.
import { clean } from './mapping.mjs';

export const HIDDEN_CASES = 2000; // frozen correctness set (optimizer_core.repo_task.CORRECTNESS_CASES)

const last = (events, type, pick = () => true) => [...events].reverse().find((e) => e.type === type && pick(e)) ?? null;

export function taskIdOf(events) {
  const started = events.find((e) => e.type === 'run_started');
  const labeled = events.find((e) => e.type === 'run_label' && typeof e.task_id === 'string');
  return clean(started?.task_id, 128) ?? clean(labeled?.task_id, 128) ?? 'logs-parse-v1';
}

export function kindOf(events) {
  const started = events.find((e) => e.type === 'run_started');
  if (String(started?.session_id ?? '').startsWith('official-')) return 'official';
  if (events.some((e) => e.type === 'run_label' && String(e.task_id ?? '').startsWith('repo-'))) return 'repo';
  return 'development';
}

// Only these recorded sessions are shown as official: PREP.md section 8 lists them, but live.py wrote its
// default "development live run" run_label text into their ledgers. Any other session keeps its ledger label.
export const OFFICIAL_SESSIONS = new Set(['official-20261017-1', 'official-20261017-2', 'official-20261017-3']);
const OFFICIAL_LABEL = 'Official session (recorded in PREP.md section 8; ledger label text is the live-run default)';

function displayLabel(events) {
  const session = events.find((e) => e.type === 'run_started')?.session_id;
  if (OFFICIAL_SESSIONS.has(session)) return OFFICIAL_LABEL;
  return clean(last(events, 'run_label')?.label) ?? '';
}

/**
 * RunSummary from the Node-parsed (hash-chain UNVERIFIED) ledger. `verified` and `ledgerVerified` stay null
 * ("checked when opened") until the Python helper has inspected the run; see withVerification.
 */
export function summarize(runId, events) {
  const started = events.find((e) => e.type === 'run_started');
  const terminal = last(events, 'terminal');
  const state = clean(terminal?.state, 48) ?? 'running';
  return {
    runId, sessionId: clean(started?.session_id, 128) ?? runId, kind: kindOf(events),
    label: displayLabel(events), state, startedUtc: started?.utc ?? events[0]?.utc ?? null,
    endedUtc: terminal?.utc ?? null, taskId: taskIdOf(events), verified: null, ledgerVerified: null,
  };
}

/** `verified` is true only when the helper verified the ledger chain and the audited receipt accepted the run. */
export function verificationOf(result) {
  return { ledgerVerified: result.ledgerVerified, verified: result.ledgerVerified && result.accepted && result.state === 'verified_done' };
}

export function artifactIds(runId) {
  return { originalArtifactId: `${runId}:original`, optimizedArtifactId: `${runId}:optimized`, proofArtifactId: `${runId}:proof` };
}

/** Small, verified source excerpts and measured pairs for the visual replay. No inputs are exposed. */
export function buildPresentation(runId, inspected) {
  const result = buildResult(runId, inspected);
  const candidates = Object.create(null);
  if (inspected.ledgerVerified === true) {
    for (const [id, source] of Object.entries(inspected.candidatePreviews ?? {})) {
      if (/^[A-Za-z0-9._-]{1,64}$/.test(id) && typeof source === 'string') candidates[id] = source.slice(0, 6000);
    }
  }
  const pairTimings = result.accepted && result.receiptAuditValid
    ? (inspected.receiptAudit?.pairTimings ?? []).filter(p =>
      Number.isFinite(p.baselineSeconds) && p.baselineSeconds > 0
      && Number.isFinite(p.candidateSeconds) && p.candidateSeconds > 0
      && p.outputsMatch === true).slice(0, 30)
    : [];
  return { candidates, pairTimings };
}

/** Accepted only when the verified ledger, the acceptance event and the audited receipt all agree. */
export function buildResult(runId, inspected) {
  const events = inspected.events;
  const started = events.find((e) => e.type === 'run_started');
  const terminal = last(events, 'terminal');
  const event = last(events, 'acceptance_result', (e) => e.accepted === true);
  const audit = inspected.receiptAudit;
  // The audited receipt must be the one the accepted event names, not just any receipt in the run.
  const accepted = Boolean(inspected.ledgerVerified && event && audit?.valid && audit.decision?.accepted === true
    && inspected.receiptId === event.receipt_id);
  const numbers = accepted ? audit.decision : null;
  const forwarded = terminal?.last_forwarded_candidate_id ?? null;
  return {
    runId, accepted,
    medianSpeedup: numbers ? numbers.median_speedup : null,
    confidenceInterval95: numbers ? [numbers.ci_lower, numbers.ci_upper] : null,
    timingPairs: numbers ? numbers.pairs : null,
    hiddenCases: HIDDEN_CASES,
    sourceSha256: accepted ? event.candidate_sha256 : null,
    receiptSha256: event?.receipt_sha256 ?? null,
    receiptAuditValid: audit?.valid === true,
    // The harness only returns accepted after the evaluation sandbox deletion is confirmed
    // (optimizer_core.harness refuses a receipt while the machine is still allocated), so a
    // measured and accepted origin implies the sandbox was deleted.
    sandboxDeleted: accepted && event.origin === 'measured',
    ...artifactIds(runId),
    state: inspected.ledgerVerified === true ? (clean(terminal?.state, 48) ?? 'running') : 'ledger_invalid',
    reason: inspected.ledgerVerified === true ? null : clean(`ledger failed hash-chain verification${inspected.error ? `: ${inspected.error}` : ''}`),
    sessionId: clean(started?.session_id, 128) ?? runId,
    label: displayLabel(events) || null,
    ledgerVerified: inspected.ledgerVerified === true,
    ledgerFinalSha256: inspected.ledgerFinalSha256 ?? null,
    taskId: taskIdOf(events),
    candidateId: accepted ? event.candidate_id : forwarded,
  };
}

function minimumSpeedup(inspected, acceptances) {
  const fromAudit = inspected.receiptAudit?.decision?.policy?.minimum_speedup;
  const fromEvent = [...acceptances].reverse().map((e) => e.acceptance?.policy?.minimum_speedup).find((v) => typeof v === 'number');
  return typeof fromAudit === 'number' ? fromAudit : (fromEvent ?? null);
}

export function buildProof(runId, inspected, result) {
  const events = inspected.events;
  const event = last(events, 'acceptance_result', (e) => e.receipt_id);
  const decision = inspected.receiptAudit?.decision ?? null;
  return {
    schema: 'no-free-wins.proof-summary.v1',
    runId, sessionId: result.sessionId, taskId: result.taskId, kind: kindOf(events),
    labels: events.filter((e) => ['run_label', 'fault_injection', 'simulation_label'].includes(e.type))
      .map((e) => ({ type: e.type, label: clean(e.label) })),
    state: result.state,
    ledger: { verified: result.ledgerVerified, finalSha256: result.ledgerFinalSha256, events: events.length,
      note: 'Hash chain verified by optimizer_core.ledger.verify_ledger; integrity is relative to a separately sealed final digest, not a signature.' },
    receipt: {
      id: event?.receipt_id ?? null, sha256: event?.receipt_sha256 ?? null,
      audit: inspected.receiptAudit ? { valid: inspected.receiptAudit.valid, error: inspected.receiptAudit.error ?? null } : null,
      note: 'Offline audit of the stored receipt against its sealed digest; not a fresh sandbox execution.',
    },
    candidate: { id: result.candidateId, sha256: result.sourceSha256 },
    metrics: decision && {
      accepted: decision.accepted, origin: decision.origin, pairs: decision.pairs, medianSpeedup: decision.median_speedup,
      ci95: [decision.ci_lower, decision.ci_upper], policy: decision.policy, reasons: decision.reasons,
    },
    hiddenCases: HIDDEN_CASES, sandboxDeleted: result.sandboxDeleted,
    original: inspected.original ? { kind: inspected.original.kind, sha256: inspected.original.sha256, generatorHashCurrent: inspected.original.generatorHashCurrent ?? null } : null,
    scope: 'Correctness applies to the tested inputs. The cheating zoo is a separate defense suite and is not part of this run.',
  };
}

/** Public facts for templated chat answers. */
export function chatFacts(runId, inspected) {
  const events = inspected.events;
  const result = buildResult(runId, inspected);
  const started = events.find((e) => e.type === 'run_started');
  const plan = last(events, 'plan');
  const budgetSource = [...events].reverse().find((e) => e.budget);
  const strategies = (plan?.strategies ?? []).map((s) => clean(s.strategy_id, 64)).filter(Boolean);
  const orders = events.filter((e) => e.type === 'worker_order').map((e) => ({ worker: clean(e.worker, 24), strategy: clean(e.strategy_id, 64) }));
  const candidates = events.filter((e) => e.type === 'candidate_created').map((e) => ({
    id: clean(e.candidate_id, 64), worker: clean(e.worker, 24), strategy: clean(e.strategy_id, 64),
    models: [...new Set((e.attempts ?? []).filter((a) => a.outcome === 'ok').map((a) => clean(a.model_id, 96)))],
  }));
  const screens = events.filter((e) => e.type === 'screen_result');
  const verdicts = events.filter((e) => e.type === 'verifier_verdict');
  const acceptances = events.filter((e) => e.type === 'acceptance_result');
  return {
    result, runId, events, kind: kindOf(events), strategies, orders, candidates, screens, verdicts, acceptances,
    sourcePreview: inspected.ledgerVerified ? inspected.candidatePreviews?.[result.candidateId] ?? '' : '',
    models: started?.models ?? null,
    minimumSpeedup: minimumSpeedup(inspected, acceptances),
    maxRounds: started?.max_rounds ?? null, budget: budgetSource?.budget ?? null,
    terminal: last(events, 'terminal'), audit: inspected.receiptAudit,
  };
}
