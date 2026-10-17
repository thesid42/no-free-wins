// Launches the existing Python CLIs (analyze / answer / approve --run / auto) as tracked jobs.
// Fixed argv arrays only: no shell, cwd = repo root, user strings are glued into one `--flag=value`
// element so a value that starts with "-" can never be read as a flag. Jobs live in memory only:
// a server restart forgets them (the Python side persists proposals and run ledgers).
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { open, readdir, stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

const RUN_ID = /^[A-Za-z0-9._-]{1,128}$/;
const MAX_JOBS = 20;
const STDOUT_MAX = 4 * 1024 * 1024; // analyst and run summaries are a few KB; refuse to buffer more than this
const STDERR_KEEP = 16 * 1024;
const ERROR_MAX = 2000;
const MINUTE = 60_000;

const STEPS_KEEP = 200;
const PROGRESS_PREFIX = 'NFW_PROGRESS ';
const PROGRESS_LINE_MAX = 16 * 1024;
const PROGRESS_STEPS = new Set(['analyze', 'repair', 'prepare', 'run', 'lessons', 'done']);
const PROGRESS_STATUSES = new Set(['started', 'ok', 'failed']);
const PROGRESS_ID = /^[A-Za-z0-9._-]{1,128}$/;
const AUTO_EXIT_WITH_RESULT = new Set([0, 3]); // verified_done, not_feasible; exit 1 (gave_up) also carries a result

export const DEFAULT_TIMEOUTS = { analyze: 10 * MINUTE, answer: 10 * MINUTE, approve: 50 * MINUTE, auto: 120 * MINUTE };
export const KILL_GRACE_MS = 30_000; // SIGINT, then SIGTERM after this, then SIGKILL after this again
export const POLL_MS = 1000;

// Timers never keep the process alive on their own: a live child or the HTTP server does that.
const timer = (fn, ms) => setTimeout(fn, ms).unref();
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Default runner: a real child process, no shell. Tests inject a fake with the same shape. */
export function spawnRunner({ file, args, cwd, env }) {
  return spawn(file, args, { cwd, env, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
}

export function newSessionId(date = new Date()) {
  const stamp = date.toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  return `ui-${stamp}-${randomBytes(3).toString('hex')}`;
}

export function newSessionPrefix(date = new Date()) {
  const stamp = date.toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  return `ui-auto-${stamp}-${randomBytes(3).toString('hex')}`;
}

const ownText = (value, max) => (typeof value === 'string' ? value.slice(0, max) : null);

/**
 * Find the run directory whose ledger starts with a run_started for this session (`match` decides which
 * session ids count). `matched` remembers earlier hits so the NEWEST one wins: directories first seen
 * later beat earlier ones, and several found in one pass are ordered by run_started time, then mtime.
 */
async function discoverRun(runsRoot, match, sinceMs, rejected, matched = new Map()) {
  let entries;
  try { entries = await readdir(runsRoot, { withFileTypes: true }); } catch { return null; }
  const fresh = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || !RUN_ID.test(entry.name) || rejected.has(entry.name) || matched.has(entry.name)) continue;
    try {
      const directory = join(runsRoot, entry.name);
      const info = await stat(directory);
      if (info.mtimeMs < sinceMs - 5000) { rejected.add(entry.name); continue; } // predates this job
      const handle = await open(join(directory, 'ledger.jsonl'), 'r');
      let head;
      try {
        const buffer = Buffer.alloc(64 * 1024);
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
        head = buffer.subarray(0, bytesRead);
      } finally { await handle.close(); }
      const end = head.indexOf(0x0a);
      if (end < 0) continue; // first line not complete yet
      const event = JSON.parse(head.subarray(0, end).toString('utf8'))?.event;
      if (event?.type === 'run_started' && typeof event.session_id === 'string' && match(event.session_id)) {
        fresh.push({ name: entry.name, mtimeMs: info.mtimeMs, utc: typeof event.utc === 'string' ? event.utc : '' });
      } else rejected.add(entry.name);
    } catch { /* no ledger yet, or half-written: try again next poll */ }
  }
  fresh.sort((a, b) => a.utc.localeCompare(b.utc) || a.mtimeMs - b.mtimeMs || a.name.localeCompare(b.name));
  for (const item of fresh) matched.set(item.name, matched.size);
  let best = null;
  for (const [name, order] of matched) if (!best || order > best.order) best = { name, order };
  return best?.name ?? null;
}

export function createJobs({
  repoRoot, runsRoot, python, runner = spawnRunner, timeouts = {}, graceMs = KILL_GRACE_MS, pollMs = POLL_MS,
}) {
  const limits = { ...DEFAULT_TIMEOUTS, ...timeouts };
  const jobs = new Map(); // insertion order = start order
  let active = null;

  const home = homedir();
  function sanitizeText(text) {
    let out = String(text);
    if (repoRoot) out = out.split(repoRoot).join('<project>');
    if (home && home !== '/') out = out.split(home).join('~');
    return out;
  }
  function sanitizeValue(value) {
    if (typeof value === 'string') return sanitizeText(value);
    if (Array.isArray(value)) return value.map(sanitizeValue);
    if (isObject(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, sanitizeValue(item)]));
    return value;
  }

  /** One NFW_PROGRESS payload -> a safe, bounded event, or null when it is not one. */
  function progressEvent(payload) {
    if (!isObject(payload) || !PROGRESS_STEPS.has(payload.step) || !PROGRESS_STATUSES.has(payload.status)) return null;
    const id = (value) => (typeof value === 'string' && PROGRESS_ID.test(value) ? value : null);
    return {
      ts: ownText(payload.ts, 64),
      step: payload.step,
      attempt: Number.isInteger(payload.attempt) && payload.attempt >= 0 && payload.attempt < 1000 ? payload.attempt : null,
      status: payload.status,
      message: sanitizeText(ownText(payload.message, 600) ?? ''),
      proposal_id: id(payload.proposal_id),
      session_id: id(payload.session_id),
      run_dir: ownText(payload.run_dir, 400) === null ? null : sanitizeText(payload.run_dir.slice(0, 400)),
      run_id: id(payload.run_id),
    };
  }

  /** Streams stderr line by line; NFW_PROGRESS lines become steps, everything else is just log text. */
  function takeProgress(job, text) {
    job.stderrPending += text;
    let end = job.stderrPending.indexOf('\n');
    while (end >= 0) {
      const line = job.stderrPending.slice(0, end).replace(/\r$/, '');
      job.stderrPending = job.stderrPending.slice(end + 1);
      addProgressLine(job, line);
      end = job.stderrPending.indexOf('\n');
    }
    if (job.stderrPending.length > PROGRESS_LINE_MAX) job.stderrPending = ''; // an endless line is not a progress line
  }

  function addProgressLine(job, line) {
    if (!line.startsWith(PROGRESS_PREFIX) || line.length > PROGRESS_LINE_MAX) return;
    let event = null;
    try { event = progressEvent(JSON.parse(line.slice(PROGRESS_PREFIX.length))); } catch { /* malformed: ignored */ }
    if (!event) return;
    job.steps.push(event);
    if (job.steps.length > STEPS_KEEP) job.steps.splice(0, job.steps.length - STEPS_KEEP);
    job.phase = event.step;
    if (!job.proposalId && event.proposal_id) job.proposalId = event.proposal_id;
  }

  function view(job) {
    return {
      jobId: job.jobId, kind: job.kind, status: job.status, startedUtc: job.startedUtc, endedUtc: job.endedUtc,
      phase: job.phase, result: job.result, error: job.error, runId: job.runId, sessionId: job.sessionId,
      proposalId: job.proposalId, sessionPrefix: job.sessionPrefix, steps: job.steps, currentRunId: job.currentRunId,
    };
  }

  function trim() {
    for (const [id, job] of jobs) {
      if (jobs.size <= MAX_JOBS) break;
      if (job !== active) jobs.delete(id);
    }
  }

  function begin(kind, args, { sessionId = null, proposalId = null, sessionPrefix = null } = {}) {
    if (active) throw Object.assign(new Error('another job is running'), { code: 'BUSY' });
    const job = {
      jobId: `job-${randomBytes(6).toString('hex')}`, kind, status: 'queued', startedUtc: new Date().toISOString(), endedUtc: null,
      phase: kind === 'approve' ? 'preparing sandbox' : 'analyzing', result: null, error: null, runId: null, sessionId, proposalId,
      cancelRequested: false, timedOut: false, child: null, timers: [], closing: false,
      sessionPrefix, steps: [], currentRunId: null, stderrPending: '',
    };
    active = job;
    jobs.set(job.jobId, job);
    trim();
    supervise(job, args).catch(() => finish(job, { status: 'failed', error: 'internal error' }));
    return view(job);
  }

  function finish(job, { status, error = null, result = null }) {
    if (job.endedUtc) return;
    for (const timer of job.timers) clearTimeout(timer);
    job.timers = [];
    job.status = status;
    job.error = error;
    job.result = result;
    job.phase = status === 'done' ? 'done' : status; // failed / cancelled keep their own label
    job.endedUtc = new Date().toISOString();
    job.child = null;
    if (active === job) active = null;
  }

  function signal(job, name) {
    try { job.child?.kill(name); } catch { /* already gone */ }
  }

  /** SIGINT first so the Python side cleans up its sandbox, then SIGTERM, then SIGKILL. */
  function escalate(job) {
    if (job.escalating || !job.child) return;
    job.escalating = true;
    signal(job, 'SIGINT');
    job.timers.push(timer(() => {
      signal(job, 'SIGTERM');
      job.timers.push(timer(() => signal(job, 'SIGKILL'), graceMs));
    }, graceMs));
  }

  async function supervise(job, args) {
    let child;
    try {
      child = await runner({ file: python, args, cwd: repoRoot, env: process.env });
    } catch (error) {
      return finish(job, { status: 'failed', error: `could not start the process (${error?.code ?? 'error'})` });
    }
    job.child = child;
    job.status = 'running';
    const stdout = [];
    let stdoutSize = 0;
    let stdoutOverflow = false;
    let stderr = '';
    child.stdout?.setEncoding?.('utf8');
    child.stderr?.setEncoding?.('utf8');
    child.stdout?.on('data', (chunk) => {
      stdoutSize += chunk.length;
      if (stdoutSize > STDOUT_MAX) { stdoutOverflow = true; return; }
      stdout.push(chunk);
    });
    child.stderr?.on('data', (chunk) => {
      stderr = (stderr + chunk).slice(-STDERR_KEEP);
      if (job.kind === 'auto') takeProgress(job, String(chunk));
    });
    child.on('error', (error) => {
      if (!job.closing) finish(job, { status: 'failed', error: `process error (${error?.code ?? 'error'})` });
    });

    job.timers.push(timer(() => { job.timedOut = true; escalate(job); }, limits[job.kind]));
    if (job.cancelRequested) escalate(job);

    let polling = true;
    const rejected = new Set();
    const startMs = Date.parse(job.startedUtc);
    const matched = new Map();
    const auto = job.kind === 'auto';
    const detect = async () => {
      if (auto) { // runs arrive one after another; the newest session-<prefix>-p<n> ledger is the current run
        if (!job.sessionPrefix) return;
        const prefix = `${job.sessionPrefix}-p`;
        const name = await discoverRun(runsRoot, (id) => id.startsWith(prefix), startMs, rejected, matched);
        if (name && !job.endedUtc) { job.currentRunId = name; job.runId = name; }
        return;
      }
      if (job.runId || !job.sessionId) return;
      const name = await discoverRun(runsRoot, (id) => id === job.sessionId, startMs, rejected);
      if (name && !job.endedUtc) { job.runId = name; job.phase = 'running agents'; }
    };
    if (job.kind === 'approve' || auto) {
      (async () => {
        while (polling && (auto || !job.runId) && !job.endedUtc) {
          await new Promise((resolve) => { timer(resolve, pollMs); });
          if (!polling) break;
          await detect().catch(() => {});
        }
      })();
    }

    child.on('close', async (code, closeSignal) => {
      if (job.closing || job.endedUtc) return;
      job.closing = true;
      polling = false;
      if (job.kind === 'approve' || auto) await detect().catch(() => {});
      if (auto) addProgressLine(job, job.stderrPending.replace(/\r$/, '')); // a last line without a newline
      let result = null;
      if (!stdoutOverflow) { try { result = JSON.parse(stdout.join('')); } catch { /* not JSON */ } }
      const tail = sanitizeText(stderr).trimEnd().slice(-ERROR_MAX);
      if (job.kind === 'analyze' && !job.proposalId && isObject(result) && typeof result.proposal_id === 'string') job.proposalId = result.proposal_id;
      if (!job.proposalId) { // a failed analysis names its proposal on the last stderr line, so the user can continue it
        try {
          const failure = JSON.parse(stderr.trim().split('\n').at(-1));
          if (typeof failure?.proposal_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(failure.proposal_id)) job.proposalId = failure.proposal_id;
        } catch { /* free-form stderr */ }
      }
      // A finished live run exits non-zero unless it reached verified_done; its summary on stdout is still the answer.
      const summaryOk = job.kind === 'approve' && isObject(result) && typeof result.state === 'string';
      if (auto && isObject(result)) {
        // exit 0 verified_done, 3 not_feasible, 1 gave_up all carry the final JSON. A cancel or timeout that
        // surfaces as exit 1 is reported as such rather than as the agents giving up.
        const interrupted = job.cancelRequested || job.timedOut;
        if (AUTO_EXIT_WITH_RESULT.has(code) || (code === 1 && !interrupted)) {
          if (!job.currentRunId) {
            const dir = typeof result.final?.run_dir === 'string' ? result.final.run_dir.split(/[\\/]/).filter(Boolean).at(-1) : null;
            if (dir && RUN_ID.test(dir)) { job.currentRunId = dir; job.runId = dir; }
          }
          return finish(job, { status: 'done', result: sanitizeValue(result) });
        }
      } else if (isObject(result) && (code === 0 || summaryOk)) return finish(job, { status: 'done', result: sanitizeValue(result) });
      if (job.timedOut) return finish(job, { status: 'failed', error: `timed out after ${Math.round(limits[job.kind] / MINUTE)} minutes\n${tail}`.slice(-ERROR_MAX) });
      if (job.cancelRequested) return finish(job, { status: 'cancelled', error: tail || null });
      const how = code === 0 ? 'output was not a JSON object' : `process exited with ${code ?? closeSignal}`;
      return finish(job, { status: 'failed', error: tail || how });
    });
  }

  return {
    startAnalyze: ({ repoUrl, prompt }) => begin('analyze', ['-m', 'agent_team', 'analyze', `--repo=${repoUrl}`, `--prompt=${prompt}`]),
    startAnswer: ({ proposalId, answer }) => begin('answer', ['-m', 'agent_team', 'analyze', `--proposal-id=${proposalId}`, `--answer=${answer}`], { proposalId }),
    startAuto: ({ repoUrl, prompt }) => {
      const sessionPrefix = newSessionPrefix();
      return begin('auto', ['-m', 'agent_team', 'auto', `--repo=${repoUrl}`, `--prompt=${prompt}`, `--session-prefix=${sessionPrefix}`], { sessionPrefix });
    },
    startApprove: ({ proposalId }) => {
      const sessionId = newSessionId();
      return begin('approve', ['-m', 'agent_team', 'approve', `--proposal-id=${proposalId}`, `--session-id=${sessionId}`, '--run'], { sessionId, proposalId });
    },
    get: (jobId) => (jobs.has(jobId) ? view(jobs.get(jobId)) : null),
    list: () => [...jobs.values()].slice(-MAX_JOBS).reverse().map(view),
    cancel(jobId) {
      const job = jobs.get(jobId);
      if (!job) return null;
      if (!job.endedUtc) { job.cancelRequested = true; escalate(job); }
      return { status: job.status, cancelRequested: job.cancelRequested };
    },
  };
}
