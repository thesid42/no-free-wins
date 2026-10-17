// Read-only client for the local replay bridge (same origin). Types: see contracts.d.ts.
// Launching runs is intentionally not implemented in this step.
const RUN_ID = /^[A-Za-z0-9._-]{1,128}$/;
const ARTIFACT_ID = /^[A-Za-z0-9._:-]{1,200}$/;
const SPEEDS = new Set([0, 1, 8]);

function runPath(runId) {
  if (typeof runId !== 'string' || !RUN_ID.test(runId)) throw new Error('Invalid run id');
  return `/api/runs/${encodeURIComponent(runId)}`;
}

async function request(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
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
    presentation(runId, signal) {
      return request(`${runPath(runId)}/presentation`, { signal });
    },
    async source(artifactId, signal) {
      const response = await fetch(this.artifactUrl(artifactId), { cache: 'no-store', signal });
      if (!response.ok) throw new Error('Source unavailable');
      return response.text();
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
    async start() { throw new Error('Launching runs is not enabled yet'); },
    async cancel() { throw new Error('Launching runs is not enabled yet'); },
  };
}
