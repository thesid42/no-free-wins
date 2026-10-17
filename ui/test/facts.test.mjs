import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildResult, summarize, verificationOf } from '../bridge/facts.mjs';

const SHA = 'a'.repeat(64);
const accepted = (receiptId) => ({ type: 'acceptance_result', accepted: true, origin: 'measured', candidate_id: 'w-r1', candidate_sha256: SHA, receipt_id: receiptId, receipt_sha256: SHA });
const decision = { accepted: true, median_speedup: 3.6, ci_lower: 3.5, ci_upper: 3.7, pairs: 30, origin: 'measured', policy: { minimum_speedup: 3 } };
const inspected = (over = {}) => ({
  ledgerVerified: true, ledgerFinalSha256: SHA, events: [{ type: 'run_started', session_id: 's' }, accepted('rcpt-A'), { type: 'terminal', state: 'verified_done' }],
  receiptAudit: { valid: true, decision }, receiptId: 'rcpt-A', ...over,
});

test('accepted requires the audited receipt to be the one the accepted event names', () => {
  assert.equal(buildResult('r', inspected()).accepted, true);
  assert.equal(buildResult('r', inspected({ receiptId: 'rcpt-B' })).accepted, false);
  assert.equal(buildResult('r', inspected({ receiptId: null })).accepted, false);
  assert.equal(buildResult('r', inspected({ receiptAudit: { valid: false, error: 'x' } })).accepted, false);
  assert.equal(buildResult('r', inspected({ receiptAudit: null })).accepted, false);
  assert.equal(buildResult('r', inspected({ receiptAudit: { valid: true, decision: { ...decision, accepted: false } } })).accepted, false);
  assert.equal(buildResult('r', inspected({ ledgerVerified: false })).accepted, false);
});

test('verified only when ledger, receipt audit and acceptance all hold', () => {
  assert.equal(verificationOf(buildResult('r', inspected())).verified, true);
  assert.equal(verificationOf(buildResult('r', inspected({ receiptId: 'rcpt-B' }))).verified, false);
  assert.equal(verificationOf(buildResult('r', inspected({ ledgerVerified: false, events: [] }))).verified, false);
  const notDone = inspected({ events: [{ type: 'run_started' }, accepted('rcpt-A'), { type: 'terminal', state: 'failed_deadline' }] });
  assert.equal(verificationOf(buildResult('r', notDone)).verified, false);
});

test('broken ledger reports ledger_invalid with a reason', () => {
  const result = buildResult('r', { ledgerVerified: false, events: [], error: 'ValueError: ledger chain mismatch', receiptAudit: null });
  assert.equal(result.state, 'ledger_invalid');
  assert.match(result.reason, /ledger chain mismatch/);
  assert.equal(buildResult('r', inspected()).reason, null);
});

test('summaries from unverified ledgers never claim verification', () => {
  const s = summarize('r', [{ type: 'run_started', session_id: 'official-x', utc: 'u' }, { type: 'terminal', state: 'verified_done', utc: 'v' }]);
  assert.equal(s.state, 'verified_done');
  assert.equal(s.verified, null);
  assert.equal(s.ledgerVerified, null);
  assert.equal(s.kind, 'official');
  assert.equal(s.label, ''); // prefix alone does not earn the official-session label
});

test('official label is an explicit allowlist', () => {
  const label = (session) => summarize('r', [{ type: 'run_started', session_id: session }, { type: 'run_label', label: 'ledger text' }]).label;
  for (const id of ['official-20261017-1', 'official-20261017-2', 'official-20261017-3']) assert.match(label(id), /^Official session \(recorded in PREP\.md section 8/);
  assert.equal(label('official-20261017-4'), 'ledger text');
  assert.equal(label('dev-1'), 'ledger text');
});
