import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { createServer } from 'node:net';
import { join } from 'node:path';
import { after, before, describe, test } from 'node:test';
import {
  acceptedEvents, addRun, baseEvents, CANDIDATE_SOURCE, makeRunsRoot, parseSse, postChat, raw, repoRoot, startApi, writeLedger, writeSealedSession,
} from './helpers.mjs';

const OFFICIAL = join(repoRoot, 'artifacts', 'runs', 'official-20261017-3');
const hasOfficial = existsSync(join(OFFICIAL, 'ledger.jsonl'));

let runsRoot;
let api;
before(async () => {
  runsRoot = makeRunsRoot();
  addRun(runsRoot, 'official-synth-1', baseEvents());
  addRun(runsRoot, 'repo-synth-1', baseEvents({ session: 'repo-synth-1', repo: true }).map((e, i) => ({ ...e, utc: e.utc.replace('10:00', '11:00') })), { candidate: null });
  addRun(runsRoot, 'dev-synth-1', baseEvents({ session: 'dev-1', terminal: false }).map((e) => ({ ...e, utc: e.utc.replace('10:00', '09:00') })));
  addRun(runsRoot, 'tampered-1', baseEvents({ session: 'dev-2' }), { candidate: CANDIDATE_SOURCE + '# tampered\n' });
  // Official label allowlist, a failed-plan run, and runs whose acceptance claim has no valid receipt.
  addRun(runsRoot, 'idle-1', baseEvents({ session: 'dev-idle', terminal: false }));
  addRun(runsRoot, 'allow-1', baseEvents({ session: 'official-20261017-2' }));
  addRun(runsRoot, 'failedplan-1', [
    { type: 'run_started', run_id: 'r', utc: '2026-10-17T08:00:00.000000Z', round: 0, session_id: 'dev-fp', max_rounds: 2, screen_bar: 3.0 },
    { type: 'run_label', run_id: 'r', utc: '2026-10-17T08:00:01.000000Z', round: 0, label: 'plan failed drill' },
    { type: 'preflight', run_id: 'r', utc: '2026-10-17T08:00:02.000000Z', round: 0, ok: true },
    { type: 'terminal', run_id: 'r', utc: '2026-10-17T08:00:03.000000Z', round: 0, state: 'failed_plan', reason: 'planner order failed', last_forwarded_candidate_id: null, last_forwarded_candidate_sha256: null },
  ], { candidate: null });
  addRun(runsRoot, 'noreceipt-1', acceptedEvents('rcpt-missing'));
  addRun(runsRoot, 'wrongsha-1', acceptedEvents('rcpt-wrong'));
  mkdirSync(join(runsRoot, '..', 'evaluations', 'rcpt-wrong'), { recursive: true });
  writeFileSync(join(runsRoot, '..', 'evaluations', 'rcpt-wrong', 'receipt.json'), '{"schema_version":1}');
  // A real ledger outside the runs root, reachable only through a symlinked run directory.
  writeLedger(join(runsRoot, '..', 'outside'), 'out-run', baseEvents({ session: 'dev-outside' }));
  symlinkSync(join(runsRoot, '..', 'outside', 'out-run'), join(runsRoot, 'link-1'), 'dir');
  api = await startApi(runsRoot);
});
after(() => api.close());

describe('health and run listing', () => {
  test('health', async () => {
    const r = await raw(api.port, '/api/health');
    assert.equal(r.status, 200);
    assert.deepEqual(r.json, { ok: true, mode: 'replay+launch', launch: true, runsRoot: 'artifacts/runs' });
  });

  test('runs: kinds, states, newest first', async () => {
    const { json } = await raw(api.port, '/api/runs');
    const byId = Object.fromEntries(json.runs.map((run) => [run.runId, run]));
    assert.equal(byId['official-synth-1'].kind, 'official');
    assert.equal(byId['repo-synth-1'].kind, 'repo');
    assert.equal(byId['dev-synth-1'].kind, 'development');
    assert.equal(byId['dev-synth-1'].state, 'running');
    assert.equal(byId['dev-synth-1'].endedUtc, null);
    assert.equal(byId['official-synth-1'].state, 'rounds_exhausted');
    assert.notEqual(byId['official-synth-1'].verified, true);
    assert.equal(byId['official-synth-1'].label, 'synthetic test run'); // not on the official-session allowlist
    assert.match(byId['allow-1'].label, /^Official session \(recorded in PREP\.md section 8/);
    assert.equal(byId['allow-1'].kind, 'official');
    assert.equal(byId['dev-synth-1'].verified === true, false);
    assert.equal(byId['repo-synth-1'].taskId, 'repo-abc123');
    assert.equal(byId['official-synth-1'].taskId, 'logs-parse-v1');
    assert.deepEqual(Object.keys(byId['official-synth-1']).sort(),
      ['endedUtc', 'kind', 'label', 'ledgerVerified', 'runId', 'sessionId', 'startedUtc', 'state', 'taskId', 'verified']);
    const starts = json.runs.map((run) => run.startedUtc);
    assert.deepEqual(starts, [...starts].sort().reverse());
    assert.equal(json.runs[0].runId, 'repo-synth-1');
  });

  test('summaries are unverified (null) until the helper has inspected the run', async () => {
    const fresh = makeRunsRoot();
    addRun(fresh, 'fresh-1', baseEvents({ session: 'dev-fresh' }));
    const server = await startApi(fresh);
    try {
      const before = (await raw(server.port, '/api/runs')).json.runs[0];
      assert.equal(before.verified, null);
      assert.equal(before.ledgerVerified, null);
      await raw(server.port, '/api/runs/fresh-1/result');
      const after = (await raw(server.port, '/api/runs')).json.runs[0];
      assert.equal(after.ledgerVerified, true);
      assert.equal(after.verified, false); // rounds_exhausted: verified chain, but nothing accepted
    } finally { await server.close(); }
  });
});

describe('containment: runs must resolve inside the runs root', () => {
  test('a symlinked run directory pointing outside is invisible and unreachable', async () => {
    assert.equal((await raw(api.port, '/api/runs/link-1/events?speed=0')).status, 404);
    assert.equal((await raw(api.port, '/api/runs/link-1/result')).status, 404);
    assert.equal((await raw(api.port, '/api/artifacts/link-1:proof')).status, 404);
    assert.equal((await raw(api.port, '/api/runs/out-run/result')).status, 404); // sibling dir is not the runs root
    const ids = (await raw(api.port, '/api/runs')).json.runs.map((run) => run.runId);
    assert.ok(!ids.includes('link-1') && !ids.includes('out-run'));
  });
});

describe('acceptance claims need an audited receipt', () => {
  for (const runId of ['noreceipt-1', 'wrongsha-1']) {
    test(`${runId}: accepted:true in the ledger is not enough`, async () => {
      const early = (await raw(api.port, '/api/runs')).json.runs.find((run) => run.runId === runId);
      assert.notEqual(early.verified, true);
      const { status, json } = await raw(api.port, `/api/runs/${runId}/result`);
      assert.equal(status, 200);
      assert.equal(json.ledgerVerified, true);
      assert.equal(json.accepted, false);
      assert.equal(json.medianSpeedup, null);
      assert.equal(json.confidenceInterval95, null);
      assert.equal(json.timingPairs, null);
      assert.equal(json.sourceSha256, null);
      assert.equal(json.receiptAuditValid, false);
      assert.equal(json.sandboxDeleted, false);
      const proof = await raw(api.port, `/api/artifacts/${runId}:proof`);
      assert.equal(proof.json.receipt.audit.valid, false);
      assert.ok(!/\/(Users|tmp|var|private)\//.test(proof.text), 'no absolute paths in the audit error');
      const late = (await raw(api.port, '/api/runs')).json.runs.find((run) => run.runId === runId);
      assert.equal(late.verified, false);
      assert.equal(late.ledgerVerified, true);
    });
  }
});

describe('path validation', () => {
  const bad = [
    '/api/runs/..%2F..%2Fetc/result', '/api/runs/../x/events', '/api/runs/%2e%2e/result', '/api/runs/a%2Fb/result',
    '/api/runs/a/b/result', `/api/runs/${'a'.repeat(129)}/result`, '/api/runs/nope/result', '/api/runs/./result',
    '/api/runs/%00/result', '/api/runs/..%5Cx/result', '/api/artifacts/..%2Fx:optimized', '/api/artifacts/official-synth-1:other',
    '/api/artifacts/official-synth-1', '/api/artifacts/%E0%A4%A:proof', '/api/nothing',
  ];
  for (const path of bad) {
    test(`404 ${path.slice(0, 50)}`, async () => {
      const r = await raw(api.port, path);
      assert.equal(r.status, 404);
      assert.equal(typeof r.json?.error, 'string');
    });
  }
});

describe('SSE replay', () => {
  test('ordered events, heartbeat skipped, end event', async () => {
    const r = await raw(api.port, '/api/runs/official-synth-1/events?speed=0');
    assert.equal(r.status, 200);
    assert.match(r.headers['content-type'], /^text\/event-stream/);
    const frames = parseSse(r.text);
    const end = frames.at(-1);
    assert.equal(end.event, 'end');
    assert.deepEqual(end.data, { state: 'rounds_exhausted' });
    const events = frames.slice(0, -1).map((f) => f.data);
    const sequences = events.map((e) => e.sequence);
    assert.deepEqual(sequences, [...sequences].sort((a, b) => a - b));
    assert.ok(!sequences.includes(4), 'heartbeat (seq 4) must be skipped');
    assert.deepEqual(events.map((e) => e.kind), ['run_started', 'run_label', 'preflight', 'plan', 'worker_order', 'worker_order', 'candidate_created', 'screen_result', 'verifier_verdict', 'terminal']);
    assert.deepEqual(events.map((e) => e.stage), ['analyze', 'analyze', 'analyze', 'analyze', 'workers', 'workers', 'workers', 'verify', 'verify', 'result']);
    assert.equal(events[7].message, 'Screen worker_a-r1: 3.70× (ranking only), correctness passed');
    assert.equal(events[9].status, 'failed');
  });

  test('allowlist: no prompts, descriptions, attempt detail or extra fields leak', async () => {
    const r = await raw(api.port, '/api/runs/official-synth-1/events?speed=0');
    for (const forbidden of ['SECRET', 'description', 'def solve', 'tokens', 'evidence_ids', 'response_sha256', 'budget']) {
      assert.ok(!r.text.includes(forbidden), `leaked ${forbidden}`);
    }
    for (const frame of parseSse(r.text).slice(0, -1)) {
      assert.deepEqual(Object.keys(frame.data).sort(), ['detail', 'kind', 'message', 'runId', 'sequence', 'stage', 'status']);
    }
  });

  test('pacing: speed compresses gaps (default speed is slower than instant)', async () => {
    const started = Date.now();
    await raw(api.port, '/api/runs/official-synth-1/events?speed=100');
    assert.ok(Date.now() - started < 2000);
  });

  test('live run is tailed until a terminal event appears', async () => {
    const pending = raw(api.port, '/api/runs/dev-synth-1/events?speed=0');
    await new Promise((done) => setTimeout(done, 400));
    writeLedger(runsRoot, 'dev-synth-1', [{ type: 'terminal', run_id: 'r', utc: '2026-10-17T09:00:30.000000Z', round: 1, state: 'failed_plan', reason: 'planner order failed' }], { append: true });
    const r = await pending;
    const frames = parseSse(r.text);
    assert.equal(frames.at(-1).event, 'end');
    assert.deepEqual(frames.at(-1).data, { state: 'failed_plan' });
    assert.equal(frames.at(-2).data.kind, 'terminal');
  });

  test('a tailing stream closes after the idle cutoff', async () => {
    const idle = await startApi(runsRoot, { sseIdleMs: 1200 });
    try {
      const frames = parseSse((await raw(idle.port, '/api/runs/idle-1/events?speed=0')).text);
      assert.equal(frames.at(-1).event, 'end');
      assert.deepEqual(frames.at(-1).data, { state: 'idle_timeout' });
    } finally { await idle.close(); }
  });

  test('a half-written trailing line is not emitted', async () => {
    addRun(runsRoot, 'half-1', baseEvents({ terminal: false }));
    const ledger = join(runsRoot, 'half-1', 'ledger.jsonl');
    appendFileSync(ledger, '{"event":{"type":"terminal"');
    const pending = raw(api.port, '/api/runs/half-1/events?speed=0');
    await new Promise((done) => setTimeout(done, 300));
    writeFileSync(ledger, (await import('node:fs')).readFileSync(ledger, 'utf8').split('\n').slice(0, -1).join('\n') + '\n');
    writeLedger(runsRoot, 'half-1', [{ type: 'terminal', run_id: 'r', utc: '2026-10-17T10:00:30.000000Z', state: 'failed_plan', reason: 'x' }], { append: true });
    const frames = parseSse((await pending).text);
    assert.equal(frames.at(-1).data.state, 'failed_plan');
  });
});

describe('result and artifacts (synthetic)', () => {
  test('no passing acceptance: accepted=false with null numbers', async () => {
    const { status, json } = await raw(api.port, '/api/runs/official-synth-1/result');
    assert.equal(status, 200);
    assert.equal(json.accepted, false);
    assert.equal(json.medianSpeedup, null);
    assert.equal(json.confidenceInterval95, null);
    assert.equal(json.timingPairs, null);
    assert.equal(json.sourceSha256, null);
    assert.equal(json.receiptAuditValid, false);
    assert.equal(json.sandboxDeleted, false);
    assert.equal(json.hiddenCases, 2000);
    assert.equal(json.ledgerVerified, true);
    assert.equal(json.state, 'rounds_exhausted');
    assert.equal(json.optimizedArtifactId, 'official-synth-1:optimized');
    assert.equal(json.originalArtifactId, 'official-synth-1:original');
    assert.equal(json.proofArtifactId, 'official-synth-1:proof');
    assert.equal(json.candidateId, 'worker_a-r1');
    assert.match(json.ledgerFinalSha256, /^[0-9a-f]{64}$/);
  });

  test('a broken ledger reports ledgerVerified=false without crashing', async () => {
    addRun(runsRoot, 'broken-1', baseEvents());
    const path = join(runsRoot, 'broken-1', 'ledger.jsonl');
    const text = (await import('node:fs')).readFileSync(path, 'utf8').replace('"plan"', '"plax"');
    writeFileSync(path, text);
    const { status, json } = await raw(api.port, '/api/runs/broken-1/result');
    assert.equal(status, 200);
    assert.equal(json.ledgerVerified, false);
    assert.equal(json.accepted, false);
    assert.equal(json.state, 'ledger_invalid');
    assert.match(json.reason, /^ledger failed hash-chain verification/);
    assert.ok(!/\/(Users|tmp|var|private)\//.test(json.reason));
    assert.equal((await raw(api.port, '/api/artifacts/broken-1:optimized')).status, 409);
  });

  test('optimized artifact: bytes match, headers are safe', async () => {
    const r = await raw(api.port, '/api/artifacts/official-synth-1%3Aoptimized');
    assert.equal(r.status, 200);
    assert.equal(r.text, CANDIDATE_SOURCE);
    assert.equal(r.headers['content-disposition'], 'attachment; filename="worker_a-r1.py"');
    assert.equal(r.headers['x-content-type-options'], 'nosniff');
  });

  test('tampered candidate: 409', async () => {
    const r = await raw(api.port, '/api/artifacts/tampered-1:optimized');
    assert.equal(r.status, 409);
    assert.equal(typeof r.json.error, 'string');
  });

  test('original artifact for a logs run is the frozen baseline', async () => {
    const r = await raw(api.port, '/api/artifacts/official-synth-1:original');
    assert.equal(r.status, 200);
    assert.match(r.text, /def solve\(/);
    assert.match(r.headers['content-disposition'], /^attachment; filename="baseline\.py"$/);
  });

  test('proof artifact is JSON with the audit fields', async () => {
    const r = await raw(api.port, '/api/artifacts/official-synth-1:proof');
    assert.equal(r.status, 200);
    assert.match(r.headers['content-disposition'], /attachment; filename="official-synth-1-proof\.json"/);
    assert.equal(r.json.ledger.verified, true);
    assert.equal(r.json.receipt.audit, null);
    assert.equal(r.json.runId, 'official-synth-1');
    assert.ok(!r.text.includes('SECRET'));
  });

  test('no optimized artifact when no candidate was forwarded: 404', async () => {
    const r = await raw(api.port, '/api/artifacts/dev-synth-1:optimized');
    assert.ok([404, 409].includes(r.status));
  });
});

describe('repo run original from a sealed session (no load_session)', () => {
  let sessions;
  let sealed;
  before(() => {
    sessions = mkdtempSync(join(tmpdir(), 'nfw-sessions-'));
    sealed = writeSealedSession(sessions, 'repo-synth-1');
    process.env.NFW_SESSIONS_ROOT = sessions; // the helper inherits this
  });
  after(() => { delete process.env.NFW_SESSIONS_ROOT; });

  test('target file is returned even though the generator hash is stale', async () => {
    const r = await raw(api.port, '/api/artifacts/repo-synth-1:original');
    assert.equal(r.status, 200);
    assert.equal(r.text, sealed.source);
    assert.equal(r.headers['content-disposition'], 'attachment; filename="core.py"');
    const proof = await raw(api.port, '/api/artifacts/repo-synth-1:proof');
    assert.equal(proof.json.original.kind, 'repo');
    assert.equal(proof.json.original.generatorHashCurrent, false);
  });

  test('a changed sealed file is refused', async () => {
    const other = writeSealedSession(sessions, 'repo-synth-2');
    addRun(runsRoot, 'repo-synth-2', baseEvents({ session: 'repo-synth-2', repo: true }), { candidate: null });
    assert.equal((await raw(api.port, '/api/artifacts/repo-synth-2:original')).status, 200);
    writeFileSync(other.bundlePath, 'AAAA');
    assert.equal((await raw(api.port, '/api/artifacts/repo-synth-2:original')).status, 404);
  });
});

describe('chat', () => {
  test('replies use the real numbers', async () => {
    const speed = await postChat(api.port, 'official-synth-1', 'What was the speedup?');
    assert.equal(speed.status, 200);
    assert.match(speed.json.reply, /3\.70×/);
    assert.match(speed.json.reply, /rounds_exhausted/);
    const cost = await postChat(api.port, 'official-synth-1', 'how much did it cost?');
    assert.match(cost.json.reply, /\$0\.0123/);
    assert.match(cost.json.reply, /4242 tokens/);
    const workers = await postChat(api.port, 'official-synth-1', 'tell me about the workers');
    assert.match(workers.json.reply, /strat_one, strat_two/);
    assert.ok(!workers.json.reply.includes('SECRET'));
    const models = await postChat(api.port, 'official-synth-1', 'which models?');
    assert.match(models.json.reply, /m-plan/);
    assert.match((await postChat(api.port, 'official-synth-1', 'why two workers?')).json.reply, /2 worker assignments/);
    assert.match((await postChat(api.port, 'official-synth-1', 'what about the zoo?')).json.reply, /separate/);
    assert.match((await postChat(api.port, 'official-synth-1', 'what did the verifier say')).json.reply, /worker_a-r1: not approved/);
    assert.match((await postChat(api.port, 'official-synth-1', 'what was checked')).json.reply, /1 passed correctness on the 2000 hidden/);
    assert.match((await postChat(api.port, 'official-synth-1', 'confidence interval?')).json.reply, /no confidence interval/);
  });

  test('"what was checked" reports only recorded stages (failed_plan run)', async () => {
    const reply = (await postChat(api.port, 'failedplan-1', 'what was checked?')).json.reply;
    assert.match(reply, /failed_plan/);
    assert.match(reply, /screening: not reached/);
    assert.match(reply, /verifier: not reached/);
    assert.match(reply, /full acceptance: not reached/);
    assert.match(reply, /no receipt exists to audit/);
    for (const invented of ['hidden cases', 'approved', 're-audited', 'timing pairs', 'sandbox', '3.0']) {
      assert.ok(!reply.includes(invented), `invented clause: ${invented}`);
    }
    const noReceipt = (await postChat(api.port, 'noreceipt-1', 'what was checked?')).json.reply;
    assert.match(noReceipt, /receipt audit FAILED/);
    assert.match(noReceipt, /was not accepted/);
  });

  test('input limits', async () => {
    assert.equal((await postChat(api.port, 'official-synth-1', '')).status, 400);
    assert.equal((await postChat(api.port, 'official-synth-1', 'x'.repeat(2001))).status, 400);
    assert.equal((await postChat(api.port, 'official-synth-1', 'x'.repeat(2000))).status, 200);
    const big = await raw(api.port, '/api/runs/official-synth-1/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: 'y', pad: 'z'.repeat(9000) }) });
    assert.equal(big.status, 413);
    const wrongType = await raw(api.port, '/api/runs/official-synth-1/chat', { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: '{"message":"hi"}' });
    assert.equal(wrongType.status, 415);
    assert.equal((await postChat(api.port, 'nope', 'hi')).status, 404);
  });

  test('same-origin check', async () => {
    assert.equal((await postChat(api.port, 'official-synth-1', 'hi', { Origin: 'http://evil.example' })).status, 403);
    assert.equal((await postChat(api.port, 'official-synth-1', 'hi', { Origin: 'null' })).status, 403);
    assert.equal((await postChat(api.port, 'official-synth-1', 'hi', { Origin: `http://localhost:${api.port}` })).status, 200);
    assert.equal((await postChat(api.port, 'official-synth-1', 'hi', { Origin: `http://localhost:${api.port + 1}` })).status, 403);
    assert.equal((await postChat(api.port, 'official-synth-1', 'hi', { Origin: `http://127.0.0.1:${api.port}` })).status, 200);
  });

  test('unexpected Host header is refused (DNS rebinding)', async () => {
    assert.equal((await raw(api.port, '/api/runs', { headers: { Host: 'evil.example' } })).status, 403);
  });
});

describe('method policy', () => {
  for (const path of ['/api/health', '/api/runs', '/api/runs/official-synth-1/result', '/api/runs/official-synth-1/events', '/api/artifacts/official-synth-1:proof']) {
    for (const method of ['POST', 'PUT', 'DELETE', 'PATCH']) {
      test(`${method} ${path} blocked`, async () => {
        const r = await raw(api.port, path, { method, headers: { 'Content-Type': 'application/json' }, body: '{"message":"x"}' });
        assert.equal(r.status, 405);
      });
    }
  }
  test('GET on the chat route is blocked', async () => {
    const r = await raw(api.port, '/api/runs/official-synth-1/chat');
    assert.equal(r.status, 405);
    assert.equal(r.headers.allow, 'POST');
  });
});

describe('real recorded run official-20261017-3', { skip: !hasOfficial && 'artifacts/runs/official-20261017-3 not present' }, () => {
  let real;
  before(async () => { real = await startApi(join(repoRoot, 'artifacts', 'runs')); });
  after(() => real.close());

  test('result carries the audited numbers', async () => {
    const { status, json } = await raw(real.port, '/api/runs/official-20261017-3/result');
    assert.equal(status, 200);
    assert.equal(json.accepted, true);
    assert.ok(Math.abs(json.medianSpeedup - 3.5434) < 5e-4);
    assert.ok(Math.abs(json.confidenceInterval95[0] - 3.4916) < 5e-4);
    assert.ok(Math.abs(json.confidenceInterval95[1] - 3.6167) < 5e-4);
    assert.equal(json.timingPairs, 30);
    assert.equal(json.hiddenCases, 2000);
    assert.equal(json.receiptAuditValid, true);
    assert.equal(json.sandboxDeleted, true);
    assert.equal(json.ledgerVerified, true);
    assert.equal(json.state, 'verified_done');
    assert.match(json.label, /^Official session \(recorded in PREP\.md section 8/);
    const listed = (await raw(real.port, '/api/runs')).json.runs.find((run) => run.runId === 'official-20261017-3');
    assert.equal(listed.verified, true); // only after the helper verified chain + audited receipt
    assert.equal(listed.ledgerVerified, true);
    assert.equal(json.receiptSha256, 'f93f6c97ca257044da9756698fc7407ed64b673724b8b460094cea7c068c68aa');
    assert.equal(json.sourceSha256, '84f630e1950a9d00f38a2239e21bf3942cfd2abdbba50a11d823113477947d33');
  });

  test('artifacts and chat', async () => {
    const optimized = await raw(real.port, '/api/artifacts/official-20261017-3:optimized');
    assert.equal(optimized.status, 200);
    assert.match(optimized.text, /def solve\(/);
    const proof = await raw(real.port, '/api/artifacts/official-20261017-3:proof');
    assert.equal(proof.json.receipt.audit.valid, true);
    assert.ok(Math.abs(proof.json.metrics.medianSpeedup - 3.5434) < 5e-4);
    const chat = await postChat(real.port, 'official-20261017-3', 'what was the speedup?');
    assert.match(chat.json.reply, /3\.543×/);
    const ci = await postChat(real.port, 'official-20261017-3', 'what is the confidence interval');
    assert.match(ci.json.reply, /3\.492–3\.617×/);
  });

  test('replay ends with verified_done', async () => {
    const frames = parseSse((await raw(real.port, '/api/runs/official-20261017-3/events?speed=0')).text);
    assert.deepEqual(frames.at(-1).data, { state: 'verified_done' });
    assert.equal(frames.at(-2).data.message, 'verified_done');
    assert.ok(frames.some((f) => f.data.message === 'Full acceptance: median 3.543×, 95% CI 3.492–3.617× over 30 pairs'));
  });
});

const REPO_DEV = join(repoRoot, 'artifacts', 'runs', 'repo-dev-1', 'ledger.jsonl');
const hasRepoDev = existsSync(REPO_DEV) && existsSync(join(repoRoot, 'private', 'sessions', 'repo-dev-1', 'session.json'));
describe('real recorded repo run repo-dev-1', { skip: !hasRepoDev && 'repo-dev-1 run or sealed session not present' }, () => {
  let real;
  before(async () => { real = await startApi(join(repoRoot, 'artifacts', 'runs')); });
  after(() => real.close());

  test('result and original are replayable', async () => {
    const { json } = await raw(real.port, '/api/runs/repo-dev-1/result');
    assert.equal(json.kind, undefined);
    assert.equal(json.accepted, true);
    assert.equal(json.receiptAuditValid, true);
    assert.equal(json.receiptSha256.slice(0, 8), '3cdafaf3');
    assert.ok(json.medianSpeedup > 8 && json.medianSpeedup < 8.1);
    const original = await raw(real.port, '/api/artifacts/repo-dev-1:original');
    assert.equal(original.status, 200);
    assert.match(original.text, /def top_words\(/);
  });
});

describe('real server.mjs: CSP and static serving', () => {
  let child;
  let port;
  before(async () => {
    port = await new Promise((resolvePort) => { const s = createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolvePort(p)); }); });
    child = spawn(process.execPath, [join(repoRoot, 'ui', 'server.mjs')], { env: { ...process.env, PORT: String(port) }, stdio: ['ignore', 'pipe', 'inherit'] });
    await new Promise((resolveStart, reject) => {
      child.stdout.on('data', () => resolveStart());
      child.on('exit', () => reject(new Error('server exited')));
    });
  });
  after(() => child.kill());

  test('CSP connect-src self, no-store, nosniff', async () => {
    const r = await raw(port, '/index.html');
    assert.equal(r.status, 200);
    const csp = r.headers['content-security-policy'];
    assert.ok(csp.includes("connect-src 'self'"));
    assert.ok(!csp.includes("connect-src 'none'"));
    assert.ok(csp.includes("default-src 'self'") && csp.includes("script-src 'self'") && csp.includes("frame-ancestors 'none'"));
    assert.equal(r.headers['cache-control'], 'no-store');
    assert.equal(r.headers['x-content-type-options'], 'nosniff');
  });

  test('static allowlist still applies; other files and methods are refused', async () => {
    assert.equal((await raw(port, '/src/main.js')).status, 200);
    assert.equal((await raw(port, '/src/contracts.d.ts')).status, 404);
    assert.equal((await raw(port, '/server.mjs')).status, 404);
    assert.equal((await raw(port, '/bridge/inspect_run.py')).status, 404);
    assert.equal((await raw(port, '/index.html', { method: 'POST' })).status, 405);
    assert.equal((await raw(port, '/src/main.js', { method: 'HEAD' })).status, 200);
  });

  test('malformed request targets get 400 and never crash the server', async () => {
    for (const path of ['//', '//[', '//%', '//%zz', '///', '//[::1']) {
      const r = await raw(port, path);
      assert.equal(r.status, 400, path);
    }
    assert.equal((await raw(port, '/api/health')).status, 200);
    assert.equal(child.exitCode, null, 'server process is still running');
  });

  test('API is served from the real runs root', async () => {
    const r = await raw(port, '/api/runs');
    assert.equal(r.status, 200);
    assert.ok(Array.isArray(r.json.runs));
    assert.equal((await raw(port, '/api/health')).json.ok, true);
  });
});
