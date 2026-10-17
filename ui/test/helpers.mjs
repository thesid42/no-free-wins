// Shared fixtures: synthetic hash-chained ledgers in a temp runs root, plus small HTTP helpers.
import { createHash } from 'node:crypto';
import { createServer, request } from 'node:http';
import { appendFileSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { deflateSync } from 'node:zlib';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createApi } from '../bridge/api.mjs';

export const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const ZERO = '0'.repeat(64);

// Test-only mirror of optimizer_core.canonical.canonical_json for ASCII fixtures. The server never uses it.
export function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}
export const sha = (data) => createHash('sha256').update(data).digest('hex');

export function writeLedger(runsRoot, runId, events, { append = false } = {}) {
  const path = join(runsRoot, runId, 'ledger.jsonl');
  mkdirSync(dirname(path), { recursive: true });
  let seq = 0;
  let previous = ZERO;
  if (append) {
    const lines = readFileSync(path, 'utf8').trim().split('\n');
    const lastRecord = JSON.parse(lines.at(-1));
    seq = lastRecord.seq;
    previous = lastRecord.sha256;
  } else writeFileSync(path, '');
  for (const event of events) {
    seq += 1;
    const body = { seq, prev_sha256: previous, event };
    previous = sha(canonical(body));
    appendFileSync(path, canonical({ ...body, sha256: previous }) + '\n');
  }
  return path;
}

export const CANDIDATE_SOURCE = 'def solve(lines):\n    return {"by_level": {}, "by_user": {}}\n';
export const CANDIDATE_SHA = sha(CANDIDATE_SOURCE);
const t = (n) => `2026-10-17T10:00:${String(n).padStart(2, '0')}.000000Z`;
const RUN = 'run-synthetic';

export function baseEvents({ session = 'official-synth-1', repo = false, terminal = true } = {}) {
  const e = (type, n, fields) => ({ type, run_id: RUN, utc: t(n), round: 0, ...fields });
  const events = [
    e('run_started', 0, {
      session_id: session, deadline_seconds: 1800.5, max_rounds: 2, screen_bar: 3.0,
      models: { planner: { primary: 'm-plan', fallback: 'm-plan-b' }, worker_a: { primary: 'm-a', fallback: 'm-a-b' }, worker_b: { primary: 'm-b', fallback: 'm-b-b' }, verifier: { primary: 'm-v', fallback: 'm-v-b' } },
      budget: { cost_usd: 0.0, max_cost_usd: 5.0, tokens_used: 0, max_tokens: 180000, attempts_used: 0, max_attempts: 60 },
      ...(repo ? { task_id: 'repo-abc123' } : {}),
    }),
    e('run_label', 0, { label: 'synthetic test run', ...(repo ? { task_id: 'repo-abc123' } : {}) }),
    e('preflight', 1, { ok: true }),
    e('heartbeat', 2, { phase: 'plan', elapsed_seconds: 0.5, budget: { cost_usd: 0.001 } }),
    e('plan', 3, {
      distinct: true, plan_sha256: 'a'.repeat(64), attempts: [{ kind: 'primary', outcome: 'ok', model_id: 'm-plan', tokens: 5, detail: 'SECRET-PROMPT' }],
      strategies: [{ strategy_id: 'strat_one', description: 'SECRET plan description one', description_sha256: 'b'.repeat(64) },
        { strategy_id: 'strat_two', description: 'SECRET plan description two', description_sha256: 'c'.repeat(64) }],
    }),
    e('worker_order', 3, { worker: 'worker_a', strategy_id: 'strat_one', primary: 'm-a', fallback: 'm-a-b', description_sha256: 'b'.repeat(64) }),
    e('worker_order', 3, { worker: 'worker_b', strategy_id: 'strat_two', primary: 'm-b', fallback: 'm-b-b', description_sha256: 'c'.repeat(64) }),
    e('candidate_created', 5, {
      round: 1, worker: 'worker_a', strategy_id: 'strat_one', candidate_id: 'worker_a-r1', candidate_sha256: CANDIDATE_SHA,
      attempts: [{ attempt: 1, kind: 'primary', outcome: 'ok', model_id: 'm-a', tokens: 99, detail: 'SECRET-PROMPT' }],
    }),
    e('screen_result', 7, {
      round: 1, candidate_id: 'worker_a-r1', candidate_sha256: CANDIDATE_SHA, status: 'screened', correctness_passed: true,
      screen_median_speedup: 3.698374, accepted: false, origin: 'measured', evidence_ids: ['x'], response_sha256: 'd'.repeat(64),
    }),
    e('verifier_verdict', 9, { round: 1, candidate_id: 'worker_a-r1', candidate_sha256: CANDIDATE_SHA, approved: false, mismatch: true, attempts: [] }),
  ];
  if (terminal) {
    events.push(e('terminal', 12, {
      round: 1, state: 'rounds_exhausted', reason: 'no acceptance after 2 rounds',
      last_forwarded_candidate_id: 'worker_a-r1', last_forwarded_candidate_sha256: CANDIDATE_SHA,
      budget: { cost_usd: 0.0123, max_cost_usd: 5.0, tokens_used: 4242, max_tokens: 180000, attempts_used: 4, max_attempts: 60 },
    }));
  }
  return events;
}

export function makeRunsRoot() {
  const base = mkdtempSync(join(tmpdir(), 'nfw-ui-test-'));
  const runsRoot = join(base, 'runs');
  mkdirSync(runsRoot);
  return runsRoot;
}

export function addRun(runsRoot, runId, events, { candidate = CANDIDATE_SOURCE } = {}) {
  writeLedger(runsRoot, runId, events);
  if (candidate !== null) {
    mkdirSync(join(runsRoot, runId, 'candidates'), { recursive: true });
    writeFileSync(join(runsRoot, runId, 'candidates', 'worker_a-r1.py'), candidate);
  }
}

/** Default runner for tests: launching must never reach a real Python CLI (Crusoe, Daytona). */
const refuseRunner = () => { throw Object.assign(new Error('test attempted to spawn a real process'), { code: 'EREFUSED' }); };

export async function startApi(runsRoot, options = {}) {
  const api = createApi({ repoRoot, runsRoot, runner: refuseRunner, ...options });
  const server = createServer(async (req, res) => {
    if (!(await api.handle(req, res))) res.writeHead(404).end('static');
  });
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  const { port } = server.address();
  return { port, close: () => new Promise((done) => { server.closeAllConnections(); server.close(done); }) };
}

/** Raw HTTP so paths are sent exactly as written (fetch would normalize "..", "%2e" and friends). */
export function raw(port, path, { method = 'GET', headers = {}, body } = {}) {
  return new Promise((resolveRequest, reject) => {
    const req = request({ host: '127.0.0.1', port, path, method, agent: false, headers: { Host: `127.0.0.1:${port}`, ...headers } }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let json = null;
        try { json = JSON.parse(text); } catch { /* not JSON */ }
        resolveRequest({ status: res.statusCode, headers: res.headers, text, json, bytes: Buffer.concat(chunks) });
      });
    });
    req.on('error', reject);
    req.end(body);
  });
}

export const postChat = (port, runId, message, headers = {}) => raw(port, `/api/runs/${runId}/chat`, {
  method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify({ message }),
});

export function parseSse(text) {
  return text.split('\n\n').filter((block) => block.trim() && !block.startsWith(':')).map((block) => {
    const fields = Object.fromEntries(block.split('\n').map((line) => [line.slice(0, line.indexOf(':')), line.slice(line.indexOf(':') + 1).trim()]));
    return { event: fields.event ?? 'message', data: JSON.parse(fields.data), id: fields.id };
  });
}

/** Write a sealed repo session (session.json + <id>.repo/*) like prepare-repo does, with a stale generator hash. */
export function writeSealedSession(sessionsRoot, sessionId, { target = 'pkg/core.py', source = 'def slow(data):\n    return data\n' } = {}) {
  const sources = { [target]: source, 'pkg/gen.py': 'def generate_input(seed, size):\n    return []\n' };
  const raw = canonical(sources);
  const files = {
    'spec.json': canonical({ target: `${target}:slow`, generator: 'pkg/gen.py:generate_input' }),
    'bundle.b64': deflateSync(Buffer.from(raw)).toString('base64'),
    'cases.json': '[]', 'expected.json': '{}', 'workloads.json': '{}', 'public.json': '{}',
  };
  mkdirSync(join(sessionsRoot, `${sessionId}.repo`), { recursive: true });
  mkdirSync(join(sessionsRoot, sessionId), { recursive: true });
  for (const [name, data] of Object.entries(files)) writeFileSync(join(sessionsRoot, `${sessionId}.repo`, name), data);
  writeFileSync(join(sessionsRoot, sessionId, 'session.json'), JSON.stringify({
    schema_version: 1, session_id: sessionId, seed: 'f'.repeat(32), seed_sha256: sha('f'.repeat(32)), generator_sha256: '0'.repeat(64),
    repo: { files: Object.fromEntries(Object.entries(files).map(([k, v]) => [k, sha(v)])), spec_sha256: sha(files['spec.json']), bundle_sha256: sha(raw), task_id: 'repo-abc123' },
  }));
  return { source, bundlePath: join(sessionsRoot, `${sessionId}.repo`, 'bundle.b64') };
}

/** Events for a run whose acceptance_result claims accepted:true with the given receipt (which may not exist). */
export function acceptedEvents(receiptId, { session = 'dev-acc' } = {}) {
  const base = baseEvents({ session, terminal: false });
  const at = (n) => `2026-10-17T10:00:${String(n).padStart(2, '0')}.000000Z`;
  return [...base,
    { type: 'acceptance_result', run_id: 'run-synthetic', utc: at(20), round: 1, candidate_id: 'worker_a-r1', candidate_sha256: CANDIDATE_SHA,
      status: 'accepted', accepted: true, origin: 'measured', receipt_id: receiptId, receipt_sha256: 'a'.repeat(64),
      acceptance: { accepted: true, median_speedup: 9.5, ci_lower: 9.1, ci_upper: 9.9, pairs: 30, origin: 'measured', policy: { minimum_speedup: 3.0 } } },
    { type: 'terminal', run_id: 'run-synthetic', utc: at(21), round: 1, state: 'verified_done', reason: 'measured receipted acceptance meets the bar',
      last_forwarded_candidate_id: 'worker_a-r1', last_forwarded_candidate_sha256: CANDIDATE_SHA },
  ];
}
