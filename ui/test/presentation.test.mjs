import assert from 'node:assert/strict';
import { test } from 'node:test';
import { lineDiff, snippet, workerState } from '../src/presentation.js';
import { chatReply } from '../bridge/chat.mjs';

test('worker graph distinguishes sequential work, queued work, failure and fallback recovery', () => {
  const run = { status: 'streaming', data: { workers: { worker_a: {}, worker_b: {} } } };
  assert.equal(workerState(run, 'worker_a').kind, 'working');
  assert.equal(workerState(run, 'worker_b').label, 'Queued');
  run.data.workers.worker_a.visualAttempt = { kind: 'primary', outcome: 'model_error' };
  assert.equal(workerState(run, 'worker_a').label, 'Primary unavailable');
  run.data.workers.worker_a.visualAttempt = { kind: 'fallback', outcome: 'ok' };
  assert.equal(workerState(run, 'worker_a').label, 'Fallback picked up the task');
  delete run.data.workers.worker_a.visualAttempt;
  run.data.workers.worker_a.candidateId = 'a-r1';
  assert.equal(workerState(run, 'worker_a').kind, 'complete');
  assert.equal(workerState(run, 'worker_b').kind, 'working');
  run.data.workers.worker_b.failed = true;
  assert.equal(workerState(run, 'worker_b').kind, 'failed');
});

test('source comparison can reconstruct both inputs and bounds large previews', () => {
  const a = 'import re\ndef original(x):\n    return x\n';
  const b = 'import re\ndef solve(x):\n    return sorted(x)\n';
  const diff = lineDiff(a, b);
  assert.equal(diff.rows.filter(r => r.kind !== 'added').map(r => r.text).join('\n'), a);
  assert.equal(diff.rows.filter(r => r.kind !== 'removed').map(r => r.text).join('\n'), b);
  assert.equal(lineDiff('a\n'.repeat(1000), 'b\n'.repeat(1000)).truncated, true);
});

test('worker snippet uses the received candidate, including significant source characters', () => {
  const code = 'import re\n# note\ndef solve(x):\n    # comment\n    return "<input>"\n';
  assert.equal(snippet(code), 'def solve(x):\n    return "<input>"');
  assert.equal(snippet(undefined), '');
});

test('contextual chat answers timing, recorded fallback and source changes without model calls', () => {
  const facts = {
    result: { accepted: true, timingPairs: 30, candidateId: 'worker_b-r1' }, acceptances: [{}],
    sourcePreview: 'from collections import Counter\ncnt = Counter()\ncnt.update(tokens)',
    events: [{ type: 'fault_injection' }, { type: 'candidate_created', worker: 'worker_a', attempts: [{ kind: 'fallback', outcome: 'ok' }] }],
  };
  assert.match(chatReply('How is timing measured?', facts), /30 matched timing pairs.*serially/);
  assert.match(chatReply('Did fallback recover?', facts), /labeled fault drill/);
  assert.match(chatReply('What changed in the code?', facts), /Counter.update/);
});
