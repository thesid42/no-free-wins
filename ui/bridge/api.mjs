// Read-only replay API over artifacts/runs/<runId>/ ledgers and audited receipts.
// Hash chains, canonical JSON and receipt audits stay in Python (inspect_run.py).
import { execFile } from 'node:child_process';
import { open, readdir, realpath, stat } from 'node:fs/promises';
import { dirname, join, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { buildProof, buildResult, chatFacts, summarize, verificationOf } from './facts.mjs';
import { chatReply } from './chat.mjs';
import { mapEvent } from './mapping.mjs';

const run = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));
const RUN_ID = /^[A-Za-z0-9._-]{1,128}$/;
const CHAT_BODY_MAX = 8 * 1024;
const CHAT_MESSAGE_MAX = 2000;
const MAX_STREAMS = 16;
const CACHE_MAX = 64;
const CACHE_TTL_MS = 5000; // receipts and candidate files can change without touching the ledger
const VERIFIED_TTL_MS = 60_000;
const GAP_CAP_MS = 2500;
const SSE_IDLE_MS = 30 * 60 * 1000; // close a tailing stream after this long with no new ledger lines
const POLL_MS = 1000;
const ARTIFACT_KINDS = new Set(['optimized', 'original', 'proof']);

export function defaultPython(repoRoot) {
  if (process.env.NFW_PYTHON) return process.env.NFW_PYTHON;
  return process.platform === 'win32' ? join(repoRoot, '.venv', 'Scripts', 'python.exe') : join(repoRoot, '.venv', 'bin', 'python');
}

class Cache {
  constructor(ttl = CACHE_TTL_MS) { this.map = new Map(); this.ttl = ttl; }
  get(key, stamp) {
    const hit = this.map.get(key);
    return hit && hit.stamp === stamp && hit.expires > Date.now() ? hit.value : undefined;
  }
  set(key, stamp, value) {
    this.map.delete(key);
    this.map.set(key, { stamp, value, expires: Date.now() + this.ttl });
    while (this.map.size > CACHE_MAX) this.map.delete(this.map.keys().next().value);
  }
}

/** Incremental ledger reader: yields only complete lines, never trusting a half-written tail. */
class LedgerTail {
  constructor(path) { this.path = path; this.offset = 0; this.broken = false; }
  async read() {
    const handle = await open(this.path, 'r');
    try {
      const { size } = await handle.stat();
      if (size <= this.offset) return [];
      const buffer = Buffer.alloc(size - this.offset);
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, this.offset);
      const end = buffer.subarray(0, bytesRead).lastIndexOf(0x0a);
      if (end < 0) return [];
      this.offset += end + 1;
      const records = [];
      for (const line of buffer.subarray(0, end).toString('utf8').split('\n')) {
        try {
          const record = JSON.parse(line);
          if (!Number.isInteger(record.seq) || !record.event || typeof record.event !== 'object') throw new Error('shape');
          records.push(record);
        } catch { this.broken = true; break; }
      }
      return records;
    } finally { await handle.close(); }
  }
}

const sleep = (ms, signal) => new Promise((resolve) => {
  if (signal.aborted) return resolve();
  const timer = setTimeout(done, ms);
  function done() { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); }
  signal.addEventListener('abort', done, { once: true });
});

export function createApi({ repoRoot, runsRoot, python = defaultPython(repoRoot), runsLabel = 'artifacts/runs', sseIdleMs = SSE_IDLE_MS }) {
  const helper = join(here, 'inspect_run.py');
  const inspectCache = new Cache();
  const summaryCache = new Cache();
  const verifiedCache = new Cache(VERIFIED_TTL_MS); // helper-verified state per run, keyed by ledger mtime+size
  const inflight = new Map();
  let streams = 0;
  let realRoot;

  async function root() {
    realRoot ??= await realpath(runsRoot);
    return realRoot;
  }

  /** Resolve a run id to its ledger path, or null for anything that is not a plain run directory. */
  async function resolveRun(runId) {
    if (typeof runId !== 'string' || !RUN_ID.test(runId) || runId.includes('..')) return null;
    try {
      const base = await root();
      const directory = await realpath(join(base, runId));
      if (!directory.startsWith(base + sep)) return null;
      const ledger = join(directory, 'ledger.jsonl');
      const info = await stat(ledger);
      return info.isFile() ? { runId, directory, ledger, stamp: `${info.mtimeMs}:${info.size}` } : null;
    } catch { return null; }
  }

  async function python_(args) {
    const { stdout } = await run(python, [helper, ...args], {
      cwd: repoRoot, timeout: 30_000, maxBuffer: 8 * 1024 * 1024,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
    });
    return JSON.parse(stdout);
  }

  async function inspectRun(found) {
    const cached = inspectCache.get(found.runId, found.stamp);
    if (cached) return cached;
    const key = `${found.runId}:${found.stamp}`;
    if (!inflight.has(key)) {
      inflight.set(key, (async () => {
        const output = await python_([await root(), found.runId]);
        if (output.error && !output.events) throw Object.assign(new Error(output.error), { code: 'helper' });
        inspectCache.set(found.runId, found.stamp, output);
        verifiedCache.set(found.runId, found.stamp, verificationOf(buildResult(found.runId, output)));
        return output;
      })().finally(() => inflight.delete(key)));
    }
    return inflight.get(key);
  }

  async function readAll(found) {
    const tail = new LedgerTail(found.ledger);
    return (await tail.read()).map((record) => record.event);
  }

  async function summary(found) {
    const cached = summaryCache.get(found.runId, found.stamp);
    if (cached) return cached;
    const value = summarize(found.runId, await readAll(found));
    summaryCache.set(found.runId, found.stamp, value);
    return value;
  }

  /** Summary plus helper-verified flags when known; null means "checked when opened". */
  let warming = Promise.resolve();
  async function summaryWithVerification(found) {
    const value = await summary(found);
    const known = verifiedCache.get(found.runId, found.stamp);
    if (known) return { ...value, ...known };
    if (value.state === 'verified_done') { // verify in the background so later listings can show true/false
      warming = warming.then(() => inspectRun(found)).catch(() => {});
    }
    return value;
  }

  // ---- responses
  function send(res, status, body, headers = {}, method = 'GET') {
    const data = Buffer.isBuffer(body) ? body : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body));
    const type = headers['Content-Type'] ?? 'application/json; charset=utf-8';
    res.writeHead(status, { ...headers, 'Content-Type': type, 'Content-Length': data.length });
    res.end(method === 'HEAD' ? undefined : data);
  }
  const fail = (res, status, error, headers = {}) => send(res, status, { error }, headers);

  // ---- routes
  async function listRuns(res, method) {
    let names = [];
    try { names = (await readdir(await root(), { withFileTypes: true })).filter((d) => d.isDirectory()).map((d) => d.name); } catch { /* no runs yet */ }
    const runs = [];
    for (const name of names) {
      const found = await resolveRun(name);
      if (found) runs.push(await summaryWithVerification(found));
    }
    runs.sort((a, b) => String(b.startedUtc ?? '').localeCompare(String(a.startedUtc ?? '')));
    send(res, 200, { runs }, {}, method);
  }

  function parseSpeed(value) {
    if (value === null || value === '') return 8;
    const speed = Number(value);
    return Number.isFinite(speed) ? Math.min(100, Math.max(0, speed)) : 8;
  }

  async function streamEvents(req, res, found, url) {
    if (req.method === 'HEAD') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', Connection: 'keep-alive' }).end();
      return;
    }
    if (streams >= MAX_STREAMS) return fail(res, 503, 'too many open event streams');
    streams += 1;
    const speed = parseSpeed(url.searchParams.get('speed'));
    const lastId = Number.parseInt(req.headers['last-event-id'] ?? '', 10) || 0;
    const abort = new AbortController();
    res.on('close', () => { abort.abort(); });
    res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' });
    res.write(': connected\n\n');
    const finish = (state) => { res.write(`event: end\ndata: ${JSON.stringify({ state })}\n\n`); res.end(); };
    const tail = new LedgerTail(found.ledger);
    let previous = null;
    let live = false;
    let idle = 0;
    let lastActivity = Date.now();
    try {
      while (!abort.signal.aborted) {
        const batch = await tail.read();
        if (batch.length) lastActivity = Date.now();
        for (const { seq, event } of batch) {
          if (abort.signal.aborted) return;
          if (seq <= lastId) continue;
          const mapped = mapEvent(found.runId, seq, event);
          if (!mapped) continue;
          const at = Date.parse(event.utc);
          if (!live && speed > 0 && previous !== null && Number.isFinite(at)) {
            await sleep(Math.min(GAP_CAP_MS, Math.max(0, (at - previous) / speed)), abort.signal);
          }
          if (Number.isFinite(at)) previous = at;
          if (abort.signal.aborted) return;
          res.write(`id: ${seq}\ndata: ${JSON.stringify(mapped)}\n\n`);
          if (event.type === 'terminal') return finish(mapped.detail.state);
        }
        if (tail.broken) return finish('ledger_invalid');
        if (live && Date.now() - lastActivity > sseIdleMs) return finish('idle_timeout');
        live = true; // caught up with the file; later events are real-time
        await sleep(POLL_MS, abort.signal);
        if (++idle % 15 === 0 && !abort.signal.aborted) res.write(': keepalive\n\n');
      }
    } catch {
      if (!res.writableEnded) res.end();
    } finally { streams -= 1; }
  }

  async function chat(req, res, found) {
    const origin = req.headers.origin;
    if (origin !== undefined && ![`http://127.0.0.1:${req.socket.localPort}`, `http://localhost:${req.socket.localPort}`].includes(origin)) return fail(res, 403, 'cross-origin request refused');
    if (!String(req.headers['content-type'] ?? '').toLowerCase().startsWith('application/json')) return fail(res, 415, 'expected application/json');
    let size = 0;
    const chunks = [];
    for await (const chunk of req) {
      size += chunk.length;
      if (size > CHAT_BODY_MAX) { req.resume(); return fail(res, 413, 'request body too large', { Connection: 'close' }); }
      chunks.push(chunk);
    }
    let message;
    try { message = JSON.parse(Buffer.concat(chunks).toString('utf8')).message; } catch { return fail(res, 400, 'invalid JSON'); }
    if (typeof message !== 'string' || !message.trim() || message.length > CHAT_MESSAGE_MAX) {
      return fail(res, 400, `message must be 1-${CHAT_MESSAGE_MAX} characters`);
    }
    const inspected = await inspectRun(found);
    if (!inspected.ledgerVerified) return fail(res, 409, 'ledger failed verification');
    send(res, 200, { reply: chatReply(message, chatFacts(found.runId, inspected)) });
  }

  async function artifact(res, runId, kind, method) {
    const found = await resolveRun(runId);
    if (!found || !ARTIFACT_KINDS.has(kind)) return fail(res, 404, 'not found');
    const attachment = (name) => ({ 'Content-Disposition': `attachment; filename="${name.replace(/[^A-Za-z0-9._-]/g, '_')}"`, 'X-Content-Type-Options': 'nosniff' });
    if (kind === 'proof') {
      const inspected = await inspectRun(found);
      const proof = buildProof(runId, inspected, buildResult(runId, inspected));
      return send(res, 200, JSON.stringify(proof, null, 2) + '\n', { ...attachment(`${runId}-proof.json`) }, method);
    }
    const out = await python_([await root(), runId, '--artifact', kind]);
    if (out.error === 'hash_mismatch' || out.error === 'ledger_invalid') return fail(res, 409, 'artifact does not match the verified ledger');
    if (out.error || typeof out.base64 !== 'string') return fail(res, 404, `${kind} artifact unavailable`);
    send(res, 200, Buffer.from(out.base64, 'base64'),
      { 'Content-Type': 'text/x-python; charset=utf-8', ...attachment(out.filename ?? `${kind}.py`) }, method);
  }

  /** @returns {Promise<boolean>} true when the request was an /api request and has been answered. */
  async function handle(req, res) {
    let url;
    try { url = new URL(req.url, 'http://localhost'); } catch { fail(res, 400, 'invalid request URL'); return true; }
    if (url.pathname !== '/api' && !url.pathname.startsWith('/api/')) return false;
    try {
      const port = req.socket.localPort;
      if (![`127.0.0.1:${port}`, `localhost:${port}`].includes(String(req.headers.host ?? '').toLowerCase())) {
        fail(res, 403, 'unexpected Host header');
        return true;
      }
      let parts;
      try { parts = url.pathname.split('/').slice(1).map(decodeURIComponent); } catch { fail(res, 404, 'not found'); return true; }
      const [, area, a, b, extra] = parts; // ['api', area, a, b, extra]
      const method = req.method;
      const isChat = area === 'runs' && b === 'chat' && extra === undefined && parts.length === 4;
      if (method === 'POST' && isChat) {
        const found = await resolveRun(a);
        if (!found) fail(res, 404, 'not found'); else await chat(req, res, found);
        return true;
      }
      if (isChat) { req.resume(); fail(res, 405, 'method not allowed', { Allow: 'POST' }); return true; }
      if (method !== 'GET' && method !== 'HEAD') { req.resume(); fail(res, 405, 'method not allowed', { Allow: 'GET, HEAD' }); return true; }
      if (area === 'health' && parts.length === 2) send(res, 200, { ok: true, mode: 'replay', runsRoot: runsLabel }, {}, method);
      else if (area === 'runs' && parts.length === 2) await listRuns(res, method);
      else if (area === 'runs' && parts.length === 4 && ['events', 'result'].includes(b)) {
        const found = await resolveRun(a);
        if (!found) fail(res, 404, 'not found');
        else if (b === 'events') await streamEvents(req, res, found, url);
        else send(res, 200, buildResult(a, await inspectRun(found)), {}, method);
      } else if (area === 'artifacts' && parts.length === 3) {
        const colon = a.lastIndexOf(':');
        if (colon < 0) fail(res, 404, 'not found'); else await artifact(res, a.slice(0, colon), a.slice(colon + 1), method);
      } else fail(res, 404, 'not found');
    } catch (error) {
      if (res.headersSent) { if (!res.writableEnded) res.end(); return true; }
      const missing = error?.code === 'ENOENT' || error?.code === 'helper' || error?.killed;
      fail(res, missing ? 503 : 500, missing ? 'inspection helper unavailable' : 'internal error');
    }
    return true;
  }

  return { handle, resolveRun };
}
