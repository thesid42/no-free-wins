import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mapEvent } from '../bridge/mapping.mjs';

const map = (event, seq = 1) => mapEvent('r1', seq, event);
const SHA = 'a'.repeat(64);

test('every mapped event has the PipelineEvent shape', () => {
  const m = map({ type: 'preflight', ok: true }, 7);
  assert.deepEqual(Object.keys(m).sort(), ['detail', 'kind', 'message', 'runId', 'sequence', 'stage', 'status']);
  assert.equal(m.runId, 'r1');
  assert.equal(m.sequence, 7);
  assert.equal(m.kind, 'preflight');
});

test('heartbeat and unknown types are skipped', () => {
  assert.equal(map({ type: 'heartbeat', phase: 'plan' }), null);
  assert.equal(map({ type: 'something_new' }), null);
});

test('analyze-stage events', () => {
  const started = map({ type: 'run_started', session_id: 's1', max_rounds: 2, deadline_seconds: 1800, models: { planner: { primary: 'p', fallback: 'f' } }, budget: { cost_usd: 1 }, screen_bar: 3 });
  assert.equal(started.stage, 'analyze');
  assert.equal(started.status, 'running');
  assert.deepEqual(Object.keys(started.detail).sort(), ['deadline_seconds', 'max_rounds', 'models', 'session_id']);
  for (const type of ['run_label', 'fault_injection']) {
    const m = map({ type, label: 'a label', secret: 'x' });
    assert.equal(m.stage, 'analyze');
    assert.deepEqual(m.detail, { label: 'a label' });
  }
  assert.equal(map({ type: 'preflight', ok: true }).stage, 'analyze');
  const plan = map({ type: 'plan', strategies: [{ strategy_id: 'one', description: 'PRIVATE', description_sha256: SHA }, { strategy_id: 'two', description: 'PRIVATE' }], attempts: [{ detail: 'PRIVATE' }] });
  assert.equal(plan.stage, 'analyze');
  assert.equal(plan.message, 'Planner proposed 2 strategies');
  assert.deepEqual(plan.detail, { strategy_ids: ['one', 'two'] });
  assert.ok(!JSON.stringify(plan).includes('PRIVATE'));
});

test('worker-stage events', () => {
  const order = map({ type: 'worker_order', worker: 'worker_a', strategy_id: 's', primary: 'p', fallback: 'f', description_sha256: SHA });
  assert.equal(order.stage, 'workers');
  assert.deepEqual(Object.keys(order.detail).sort(), ['fallback', 'primary', 'strategy_id', 'worker']);
  const created = map({ type: 'candidate_created', worker: 'worker_a', candidate_id: 'worker_a-r1', candidate_sha256: SHA, strategy_id: 's', attempts: [{ kind: 'primary', outcome: 'ok', model_id: 'm', tokens: 5, detail: 'PRIVATE' }] });
  assert.equal(created.message, 'worker_a wrote candidate worker_a-r1');
  assert.deepEqual(created.detail.attempts, [{ kind: 'primary', outcome: 'ok', model_id: 'm' }]);
  assert.ok(!JSON.stringify(created).includes('PRIVATE'));
  assert.equal(map({ type: 'worker_failed', worker: 'worker_b', strategy_id: 's', attempts: [] }).stage, 'workers');
  assert.equal(map({ type: 'duplicate_candidate', worker: 'worker_b', candidate_id: 'worker_b-r1', candidate_sha256: SHA, duplicate_of: 'x' }).stage, 'workers');
});

test('screen_result status and message', () => {
  const ok = map({ type: 'screen_result', candidate_id: 'worker_a-r1', status: 'screened', correctness_passed: true, screen_median_speedup: 3.698374, evidence_ids: ['x'], response_sha256: SHA });
  assert.equal(ok.stage, 'verify');
  assert.equal(ok.status, 'passed');
  assert.equal(ok.message, 'Screen worker_a-r1: 3.70× (ranking only), correctness passed');
  assert.deepEqual(Object.keys(ok.detail).sort(), ['candidate_id', 'correctness_passed', 'screen_median_speedup', 'status']);
  const bad = map({ type: 'screen_result', candidate_id: 'worker_a-r1', status: 'screened', correctness_passed: false, screen_median_speedup: null });
  assert.equal(bad.status, 'failed');
  assert.match(bad.message, /correctness failed/);
  assert.equal(map({ type: 'screen_result', candidate_id: 'c', status: 'budget_exhausted', correctness_passed: true }).status, 'failed');
});

test('verifier_verdict status and message', () => {
  const yes = map({ type: 'verifier_verdict', candidate_id: 'worker_a-r1', approved: true, mismatch: false, verdict_id: 'v', attempts: [] });
  assert.equal(yes.stage, 'verify');
  assert.equal(yes.status, 'passed');
  assert.equal(yes.message, 'Verifier approved worker_a-r1 (hash-bound)');
  assert.deepEqual(Object.keys(yes.detail).sort(), ['approved', 'candidate_id', 'mismatch']);
  const no = map({ type: 'verifier_verdict', candidate_id: 'worker_a-r1', approved: false, mismatch: true });
  assert.equal(no.status, 'failed');
});

test('acceptance_result status, message and detail', () => {
  const m = map({
    type: 'acceptance_result', candidate_id: 'worker_a-r1', status: 'accepted', accepted: true, origin: 'measured', receipt_sha256: SHA, receipt_id: 'r',
    evidence_ids: ['x'], response_sha256: SHA, verdict_id: 'v',
    acceptance: { accepted: true, ci_lower: 3.491640891, ci_upper: 3.616742188, median_speedup: 3.54339755, origin: 'measured', pairs: 30, policy: { pairs: 30 }, reasons: [] },
  });
  assert.equal(m.stage, 'race');
  assert.equal(m.status, 'passed');
  assert.equal(m.message, 'Full acceptance: median 3.543×, 95% CI 3.492–3.617× over 30 pairs');
  assert.deepEqual(Object.keys(m.detail).sort(), ['acceptance', 'accepted', 'candidate_id', 'origin', 'receipt_sha256', 'status']);
  assert.deepEqual(Object.keys(m.detail.acceptance).sort(), ['ci_lower', 'ci_upper', 'median_speedup', 'pairs']);
  const rejected = map({ type: 'acceptance_result', candidate_id: 'c', status: 'bar_not_met', accepted: false, acceptance: { pairs: 30, median_speedup: 2.9, ci_lower: 2.8, ci_upper: 3.0 } });
  assert.equal(rejected.status, 'failed');
});

test('terminal status by state', () => {
  const done = map({ type: 'terminal', state: 'verified_done', reason: 'ok', last_forwarded_candidate_id: 'w-r1' });
  assert.equal(done.stage, 'result');
  assert.equal(done.status, 'passed');
  assert.equal(done.message, 'verified_done');
  assert.deepEqual(done.detail, { state: 'verified_done', reason: 'ok', last_forwarded_candidate_id: 'w-r1' });
  assert.equal(map({ type: 'terminal', state: 'simulation_completed', reason: 'r' }).status, 'passed');
  const failed = map({ type: 'terminal', state: 'failed_deadline', reason: 'deadline reached' });
  assert.equal(failed.status, 'failed');
  assert.equal(failed.message, 'failed_deadline: deadline reached');
});

test('ledger text is sanitized and clipped', () => {
  const m = map({ type: 'run_label', label: 'a\u0000b\nc' + 'x'.repeat(500) });
  assert.ok(!/[\u0000-\u001f]/.test(m.message));
  assert.ok(m.detail.label.length <= 201);
});
