// Client for the local bridge (same origin). Types: see contracts.d.ts.
// Replay endpoints are read-only. Launch endpoints (analyze, answer, approve, auto) spend credits, so each one
// requires an explicit `confirmSpend: true` that main.js only passes from a user-confirmed action.
const RUN_ID = /^[A-Za-z0-9._-]{1,128}$/;
const ARTIFACT_ID = /^[A-Za-z0-9._:-]{1,200}$/;
const JOB_ID = /^[A-Za-z0-9._:-]{1,200}$/;
const SPEEDS = new Set([0, 1, 8]);
const POLL_MS = 2000;
const MAX_POLL_FAILURES = 5;
// Same shape as the server (optimizer_core/repo_task.py _GITHUB) plus the literal bundled example.
const GITHUB = /^https:\/\/github\.com\/([A-Za-z0-9_.-]{1,100})\/([A-Za-z0-9_.-]{1,100}?)(?:\.git)?(?:@([A-Za-z0-9._/-]{1,200}))?$/;
export const EXAMPLE_REPO = 'examples/textstats';
const JOB_STATES = new Set(['queued', 'running', 'done', 'failed', 'cancelled']);

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** Returns an error message, or '' when the repo reference is acceptable. */
export function repoUrlProblem(value) {
  if (typeof value !== 'string' || !value) return 'Paste a public GitHub repo link.';
  if (value === EXAMPLE_REPO) return '';
  const match = GITHUB.exec(value);
  const message = 'Use https://github.com/owner/name (optionally .git or @ref), or examples/textstats.';
  if (!match) return message;
  const [, owner, name, ref] = match;
  if ([owner, name].some((part) => part === '.' || part === '..')) return message;
  if (ref !== undefined && (/^[-/]/.test(ref) || ref.endsWith('/') || ref.includes('..') || ref.includes('//') || ref.endsWith('.lock'))) return message;
  return '';
}

export const isJobActive = (status) => status === 'queued' || status === 'running';

function runPath(runId) {
  if (typeof runId !== 'string' || !RUN_ID.test(runId)) throw new Error('Invalid run id');
  return `/api/runs/${encodeURIComponent(runId)}`;
}

function jobPath(jobId) {
  if (typeof jobId !== 'string' || !JOB_ID.test(jobId)) throw new Error('Invalid job id');
  return `/api/jobs/${encodeURIComponent(jobId)}`;
}

function proposalPath(proposalId) {
  if (typeof proposalId !== 'string' || !JOB_ID.test(proposalId)) throw new Error('Invalid proposal id');
  return `/api/analyses/${encodeURIComponent(proposalId)}`;
}

async function request(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  if (!response.ok) {
    let detail = '';
    try {
      const body = await response.json();
      if (typeof body?.error === 'string') detail = body.error.slice(0, 300);
    } catch { /* non-JSON error body */ }
    if (response.status === 409) throw new ApiError('Another job is running', 409);
    throw new ApiError(detail || `Request failed (${response.status})`, response.status);
  }
  return response.json();
}

function post(url, body, signal) {
  return request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
}

function sleep(ms, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    const done = () => { clearTimeout(timer); signal?.removeEventListener('abort', done); resolve(); };
    const timer = setTimeout(done, ms);
    signal?.addEventListener('abort', done, { once: true });
  });
}

function jobId(body) {
  if (typeof body?.jobId !== 'string' || !JOB_ID.test(body.jobId)) throw new ApiError('Unexpected response from the launch API');
  return body;
}

function normalizeJob(body) {
  if (!body || typeof body !== 'object' || !JOB_STATES.has(body.status)) throw new ApiError('Unexpected job response');
  return body;
}

async function* stream(runId, signal, options = {}) {
  const speed = SPEEDS.has(options.speed) ? options.speed : 8;
  if (signal?.aborted) return;
  const source = new EventSource(`${runPath(runId)}/events?speed=${encodeURIComponent(speed)}`);
  const queue = [];
  let wake = null;
  let finished = false;
  let failure = null;
  const poke = () => { if (wake) { wake(); wake = null; } };
  const finish = (error) => { finished = true; failure = error || null; source.close(); poke(); };
  source.onmessage = (message) => {
    try {
      queue.push(JSON.parse(message.data));
    } catch {
      return finish(new Error('Malformed event from the replay stream'));
    }
    poke();
  };
  source.addEventListener('end', (message) => {
    try { options.onEnd?.(JSON.parse(message.data)?.state); } catch { options.onEnd?.(undefined); }
    finish();
  });
  // EventSource would silently reconnect and replay from the start, so any error ends the stream.
  source.onerror = () => finish(new Error('Replay stream interrupted'));
  const onAbort = () => finish();
  signal?.addEventListener('abort', onAbort, { once: true });
  try {
    while (!signal?.aborted) {
      if (queue.length) { yield queue.shift(); continue; }
      if (finished) break;
      await new Promise((resolve) => { wake = resolve; });
    }
    if (failure && !signal?.aborted) throw failure;
  } finally {
    signal?.removeEventListener('abort', onAbort);
    source.close();
  }
}

export function createAdapter() {
  return {
    async health(signal) {
      return request('/api/health', { signal });
    },
    async runs(signal) {
      const body = await request('/api/runs', { signal });
      return Array.isArray(body?.runs) ? body.runs : [];
    },
    events: stream,
    result(runId, signal) {
      return request(`${runPath(runId)}/result`, { signal });
    },
    async chat(runId, message, signal) {
      const body = await request(`${runPath(runId)}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
        signal,
      });
      if (typeof body?.reply !== 'string') throw new Error('Unexpected chat reply');
      return body.reply;
    },
    artifactUrl(artifactId) {
      if (typeof artifactId !== 'string' || !ARTIFACT_ID.test(artifactId)) throw new Error('Invalid artifact id');
      return `/api/artifacts/${encodeURIComponent(artifactId)}`;
    },
    // Launch API. Callers must have shown the user a confirmation first; confirmSpend is how that is declared.
    async analyze(repoUrl, prompt, signal) {
      const problem = repoUrlProblem(repoUrl);
      if (problem) throw new Error(problem);
      if (typeof prompt !== 'string' || !prompt.trim()) throw new Error('Add a prompt for the analyst.');
      return jobId(await post('/api/analyses', { repoUrl, prompt: prompt.trim(), confirmSpend: true }, signal));
    },
    /** Autonomous job: analyze, self-repair, run and re-plan with no further questions. One confirmed click covers all of it. */
    async auto(repoUrl, prompt, signal) {
      const problem = repoUrlProblem(repoUrl);
      if (problem) throw new Error(problem);
      if (typeof prompt !== 'string' || !prompt.trim()) throw new Error('Add a prompt for the agents.');
      return jobId(await post('/api/auto', { repoUrl, prompt: prompt.trim(), confirmSpend: true }, signal));
    },
    async answer(proposalId, text, signal) {
      if (typeof text !== 'string' || !text.trim()) throw new Error('Write an answer first.');
      return jobId(await post(`${proposalPath(proposalId)}/answer`, { answer: text.trim(), confirmSpend: true }, signal));
    },
    async approve(proposalId, signal) {
      return jobId(await post(`${proposalPath(proposalId)}/approve`, { confirmSpend: true }, signal));
    },
    async job(id, signal) {
      return normalizeJob(await request(jobPath(id), { signal }));
    },
    async cancel(id, signal) {
      return post(`${jobPath(id)}/cancel`, {}, signal);
    },
    /** Polls a job every 2 s until it ends. Resolves with the last snapshot (null if aborted). */
    async pollJob(id, signal, onUpdate) {
      let failures = 0;
      while (!signal?.aborted) {
        try {
          const snapshot = await this.job(id, signal);
          failures = 0;
          onUpdate?.(snapshot);
          if (!isJobActive(snapshot.status)) return snapshot;
        } catch (failure) {
          if (signal?.aborted) return null;
          if (failure instanceof ApiError && failure.status === 404) throw failure;
          if (++failures >= MAX_POLL_FAILURES) throw failure;
        }
        await sleep(POLL_MS, signal);
      }
      return null;
    },
  };
}
