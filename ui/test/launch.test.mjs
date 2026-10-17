// Launch routes: every test injects a fake runner, so no real Python CLI (Crusoe, Daytona) is ever spawned.
import assert from 'node:assert/strict';
import { EventEmitter, once } from 'node:events';
import { utimesSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname } from 'node:path';
import { PassThrough } from 'node:stream';
import { describe, test } from 'node:test';
import { createApi, defaultPython } from '../bridge/api.mjs';
import { addRun, baseEvents, makeRunsRoot, raw, repoRoot, startApi, writeLedger } from './helpers.mjs';

class FakeChild extends EventEmitter {
  constructor(args) {
    super();
    this.args = args;
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.signals = [];
    this.ended = false;
  }
  kill(name) { this.signals.push(name); return true; }
  async exit(code, { stdout = '', stderr = '', signal = null } = {}) {
    if (this.ended) return;
    this.ended = true;
    const drained = [once(this.stdout, 'end'), once(this.stderr, 'end')];
    this.stdout.end(stdout);
    this.stderr.end(stderr);
    await Promise.all(drained);
    this.emit('close', code, signal);
  }
}

/** Fake command runner: records calls and hands back controllable children. `gate` delays the spawn itself. */
function fakeRunner({ gate = null, onSpawn = null } = {}) {
  const runner = async (call) => {
    runner.calls.push(call);
    if (gate) await gate;
    const child = new FakeChild(call.args);
    runner.children.push(child);
    onSpawn?.(child, call);
    return child;
  };
  runner.calls = [];
  runner.children = [];
  return runner;
}

const json = (port, path, body, headers = {}) => raw(port, path, {
  method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body),
});
const analyze = (port, repoUrl, prompt = 'make it faster', extra = {}) => json(port, '/api/analyses', { repoUrl, prompt, confirmSpend: true, ...extra });
const getJob = async (port, jobId) => (await raw(port, `/api/jobs/${jobId}`)).json;
async function until(check, what = 'condition') {
  for (let i = 0; i < 400; i += 1) {
    const value = await check();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(`timed out waiting for ${what}`);
}
const untilStatus = (port, jobId, ...statuses) => until(async () => { const job = await getJob(port, jobId); return statuses.includes(job.status) ? job : null; }, `job ${jobId} to be ${statuses}`);
const nextChild = (runner, count = 1) => until(() => (runner.children.length >= count ? runner.children[count - 1] : null), 'a child process');

async function withApi(options, body) {
  const runsRoot = makeRunsRoot();
  const runner = options.runner ?? fakeRunner();
  const api = await startApi(runsRoot, { ...options, runner });
  try { await body({ api, port: api.port, runner, runsRoot }); } finally { await api.close(); }
}

const ANALYST_RESULT = { proposal_id: 'prop-abc', status: 'proposal', questions: [], suggestions: [], reason: '', proposal: { target: 'pkg/core.py:slow' } };

describe('repoUrl validation', () => {
  const good = [
    'https://github.com/octo/repo', 'https://github.com/octo/repo.git', 'https://github.com/octo/repo@main',
    'https://github.com/octo/repo.git@v1.2.3', 'https://github.com/octo/re.po-x_y@feature/branch-1', 'https://github.com/a.b/c_d', 'examples/textstats',
  ];
  const bad = [
    'http://github.com/octo/repo', 'https://gitlab.com/octo/repo', 'https://github.com.evil.example/octo/repo', 'https://github.com/octo',
    'https://github.com/../repo', 'https://github.com/octo/..', 'https://github.com/./repo', 'https://github.com/octo/repo@../x', 'https://github.com/octo/repo/../x',
    'https://github.com/octo/repo with space', ' https://github.com/octo/repo', 'https://github.com/octo/repo\n', 'https://github.com/octo/repo/extra',
    'https://github.com/octo/repo@', '/etc/passwd', '.', '..', '../no-free-wins', 'examples/other', 'examples/textstats/', 'examples/textstats/..', './examples/textstats',
    'file:///etc/passwd', 'git@github.com:octo/repo.git', '-rf', '', 7, null, undefined, ['https://github.com/octo/repo'],
  ];
  test('good forms are accepted', async () => {
    await withApi({ runner: fakeRunner() }, async ({ port, runner }) => {
      for (const [index, url] of good.entries()) {
        const r = await analyze(port, url);
        assert.equal(r.status, 202, url);
        const child = await nextChild(runner, index + 1);
        assert.equal(child.args[3], `--repo=${url}`);
        await child.exit(0, { stdout: JSON.stringify(ANALYST_RESULT) });
        await untilStatus(port, r.json.jobId, 'done');
      }
    });
  });
  test('bad forms are rejected and nothing is spawned', async () => {
    await withApi({}, async ({ port, runner }) => {
      for (const url of bad) {
        const r = await analyze(port, url);
        assert.equal(r.status, 400, String(url));
        assert.match(r.json.error, /repoUrl/);
      }
      assert.equal(runner.calls.length, 0);
    });
  });
});

describe('input validation', () => {
  test('confirmSpend must be literally true', async () => {
    await withApi({}, async ({ port, runner }) => {
      const message = 'confirm spending: analysis uses Crusoe credits (capped by analysis.max_cost_usd)';
      for (const body of [
        { repoUrl: 'examples/textstats', prompt: 'x' },
        { repoUrl: 'examples/textstats', prompt: 'x', confirmSpend: false },
        { repoUrl: 'examples/textstats', prompt: 'x', confirmSpend: 'true' },
        { repoUrl: 'examples/textstats', prompt: 'x', confirmSpend: 1 },
      ]) {
        const r = await json(port, '/api/analyses', body);
        assert.equal(r.status, 400);
        assert.equal(r.json.error, message);
      }
      assert.equal((await json(port, '/api/analyses/prop-abc/answer', { answer: 'x' })).status, 400);
      assert.equal((await json(port, '/api/analyses/prop-abc/approve', {})).status, 400);
      assert.equal(runner.calls.length, 0);
    });
  });
  test('prompt, answer and proposal id limits', async () => {
    await withApi({}, async ({ port, runner }) => {
      for (const prompt of ['', '   ', 'x'.repeat(4001), 5, null, 'bad\0nul']) {
        assert.equal((await analyze(port, 'examples/textstats', prompt)).status, 400, String(prompt).slice(0, 10));
      }
      for (const answer of ['', ' ', 'x'.repeat(4001), {}, 'a\0b']) {
        assert.equal((await json(port, '/api/analyses/prop-abc/answer', { answer, confirmSpend: true })).status, 400);
      }
      for (const id of ['a%20b', 'a.b', 'x'.repeat(129), 'a%2Fb', '%00']) {
        assert.equal((await json(port, `/api/analyses/${id}/answer`, { answer: 'x', confirmSpend: true })).status, 400, id);
        assert.equal((await json(port, `/api/analyses/${id}/approve`, { confirmSpend: true })).status, 400, id);
      }
      assert.equal(runner.calls.length, 0);
    });
  });
  test('boundary lengths are accepted', async () => {
    await withApi({}, async ({ port, runner }) => {
      const r = await analyze(port, 'examples/textstats', 'x'.repeat(4000));
      assert.equal(r.status, 202);
      assert.equal((await nextChild(runner)).args[4].length, '--prompt='.length + 4000);
    });
  });
  test('body must be JSON, an object and at most 16 KB', async () => {
    await withApi({}, async ({ port, runner }) => {
      const plain = await raw(port, '/api/analyses', { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: '{}' });
      assert.equal(plain.status, 415);
      const none = await raw(port, '/api/analyses', { method: 'POST', body: '{}' });
      assert.equal(none.status, 415);
      for (const body of ['not json', 'null', '[]', '"x"']) {
        const r = await raw(port, '/api/analyses', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
        assert.equal(r.status, 400, body);
      }
      const big = await json(port, '/api/analyses', { repoUrl: 'examples/textstats', prompt: 'x', confirmSpend: true, pad: 'y'.repeat(17 * 1024) });
      assert.equal(big.status, 413);
      assert.equal(runner.calls.length, 0);
    });
  });
  test('wrong methods on launch routes are 405 with Allow: POST', async () => {
    await withApi({}, async ({ port }) => {
      for (const path of ['/api/analyses', '/api/analyses/prop-abc/answer', '/api/analyses/prop-abc/approve', '/api/jobs/job-000000000000/cancel']) {
        for (const method of ['GET', 'PUT', 'DELETE']) {
          const r = await raw(port, path, { method });
          assert.equal(r.status, 405, `${method} ${path}`);
          assert.equal(r.headers.allow, 'POST');
        }
      }
    });
  });
});

describe('argv shape (no shell, values cannot become flags)', () => {
  test('analyze, answer and approve use fixed argv arrays in the repo root', async () => {
    await withApi({}, async ({ port, runner }) => {
      const a = await analyze(port, 'https://github.com/octo/repo@main', 'speed up parsing');
      const first = await nextChild(runner, 1);
      assert.deepEqual(runner.calls[0].args, ['-m', 'agent_team', 'analyze', '--repo=https://github.com/octo/repo@main', '--prompt=speed up parsing']);
      assert.equal(runner.calls[0].file, defaultPython(repoRoot));
      assert.equal(runner.calls[0].cwd, repoRoot);
      assert.equal(runner.calls[0].shell, undefined);
      assert.deepEqual(Object.keys(runner.calls[0]).sort(), ['args', 'cwd', 'env', 'file']);
      await first.exit(0, { stdout: JSON.stringify(ANALYST_RESULT) });
      await untilStatus(port, a.json.jobId, 'done');

      const b = await json(port, '/api/analyses/prop-abc/answer', { answer: 'the hot loop in core.py', confirmSpend: true });
      assert.equal(b.status, 202);
      const second = await nextChild(runner, 2);
      assert.deepEqual(runner.calls[1].args, ['-m', 'agent_team', 'analyze', '--proposal-id=prop-abc', '--answer=the hot loop in core.py']);
      await second.exit(0, { stdout: JSON.stringify(ANALYST_RESULT) });
      await untilStatus(port, b.json.jobId, 'done');

      const c = await json(port, '/api/analyses/prop-abc/approve', { confirmSpend: true });
      assert.equal(c.status, 202);
      assert.match(c.json.sessionId, /^ui-\d{8}T\d{6}Z-[0-9a-f]{6}$/);
      assert.deepEqual(Object.keys(c.json).sort(), ['jobId', 'sessionId']);
      await nextChild(runner, 3);
      assert.deepEqual(runner.calls[2].args, ['-m', 'agent_team', 'approve', '--proposal-id=prop-abc', `--session-id=${c.json.sessionId}`, '--run']);
      assert.equal(runner.calls[2].cwd, repoRoot);
    });
  });
  test('values that look like flags or shell syntax stay inside one element', async () => {
    await withApi({}, async ({ port, runner }) => {
      let count = 0;
      for (const prompt of ['--help', '-h', '--prompt=--help', '--repo=/etc', '$(touch /tmp/x); `id` | cat && "q" \'q\'\nline2', '- leading dash']) {
        const r = await analyze(port, 'examples/textstats', prompt);
        assert.equal(r.status, 202);
        count += 1;
        const child = await nextChild(runner, count);
        assert.deepEqual(child.args, ['-m', 'agent_team', 'analyze', '--repo=examples/textstats', `--prompt=${prompt}`]);
        assert.equal(child.args.length, 5);
        await child.exit(0, { stdout: '{}' });
        await untilStatus(port, r.json.jobId, 'done');
      }
      const r = await json(port, '/api/analyses/-dash-id/answer', { answer: '--help', confirmSpend: true });
      assert.equal(r.status, 202);
      assert.deepEqual((await nextChild(runner, count + 1)).args, ['-m', 'agent_team', 'analyze', '--proposal-id=-dash-id', '--answer=--help']);
    });
  });
});

describe('job lifecycle and the single-job lock', () => {
  test('queued, running, done with the parsed result', async () => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    await withApi({ runner: fakeRunner({ gate }) }, async ({ port, runner }) => {
      const r = await analyze(port, 'examples/textstats');
      assert.equal(r.status, 202);
      assert.match(r.json.jobId, /^job-[0-9a-f]{12}$/);
      const queued = await getJob(port, r.json.jobId);
      assert.equal(queued.status, 'queued');
      assert.equal(queued.kind, 'analyze');
      assert.equal(queued.phase, 'analyzing');
      assert.equal(queued.endedUtc, null);
      assert.equal(queued.result, null);
      assert.deepEqual(Object.keys(queued).sort(), ['currentRunId', 'endedUtc', 'error', 'jobId', 'kind', 'phase', 'proposalId', 'result', 'runId', 'sessionId', 'sessionPrefix', 'startedUtc', 'status', 'steps']);
      assert.deepEqual(queued.steps, []);
      assert.equal(queued.currentRunId, null);
      release();
      const child = await nextChild(runner);
      const running = await untilStatus(port, r.json.jobId, 'running');
      assert.equal(running.phase, 'analyzing');
      await child.exit(0, { stdout: JSON.stringify(ANALYST_RESULT, null, 2), stderr: 'The analyst proposes...' });
      const done = await untilStatus(port, r.json.jobId, 'done');
      assert.deepEqual(done.result, ANALYST_RESULT);
      assert.equal(done.phase, 'done');
      assert.equal(done.error, null);
      assert.match(done.endedUtc, /^\d{4}-\d\d-\d\dT/);
      assert.equal(done.proposalId, 'prop-abc');
      assert.equal(done.runId, null);
      assert.equal(done.sessionId, null);
    });
  });

  test('a second launch gets 409 while one job is active, and works after it ends', async () => {
    await withApi({}, async ({ port, runner }) => {
      const first = await analyze(port, 'examples/textstats');
      assert.equal(first.status, 202);
      const child = await nextChild(runner);
      for (const r of [
        await analyze(port, 'examples/textstats'),
        await json(port, '/api/analyses/prop-abc/answer', { answer: 'x', confirmSpend: true }),
        await json(port, '/api/analyses/prop-abc/approve', { confirmSpend: true }),
      ]) {
        assert.equal(r.status, 409);
        assert.deepEqual(r.json, { error: 'another job is running' });
      }
      assert.equal(runner.calls.length, 1);
      await child.exit(0, { stdout: '{}' });
      await untilStatus(port, first.json.jobId, 'done');
      assert.equal((await analyze(port, 'examples/textstats')).status, 202);
    });
  });

  test('the lock is also released by failures', async () => {
    await withApi({}, async ({ port, runner }) => {
      const first = await analyze(port, 'examples/textstats');
      await (await nextChild(runner)).exit(1, { stderr: 'boom' });
      await untilStatus(port, first.json.jobId, 'failed');
      assert.equal((await analyze(port, 'examples/textstats')).status, 202);
    });
  });

  test('job list is newest first and capped at 20', async () => {
    await withApi({}, async ({ port, runner }) => {
      const ids = [];
      for (let i = 0; i < 23; i += 1) {
        const r = await analyze(port, 'examples/textstats', `p${i}`);
        ids.push(r.json.jobId);
        await (await nextChild(runner, i + 1)).exit(0, { stdout: '{}' });
        await untilStatus(port, r.json.jobId, 'done');
      }
      const { jobs } = (await raw(port, '/api/jobs')).json;
      assert.equal(jobs.length, 20);
      assert.equal(jobs[0].jobId, ids.at(-1));
      assert.equal(jobs.at(-1).jobId, ids[3]);
      assert.equal((await raw(port, `/api/jobs/${ids[0]}`)).status, 404);
      assert.equal((await raw(port, '/api/jobs/not-a-job')).status, 404);
    });
  });

  test('a spawn failure is a failed job, not a crash', async () => {
    const runner = () => { throw Object.assign(new Error(`spawn ${repoRoot}/.venv/bin/python ENOENT`), { code: 'ENOENT' }); };
    await withApi({ runner }, async ({ port }) => {
      const r = await analyze(port, 'examples/textstats');
      const job = await untilStatus(port, r.json.jobId, 'failed');
      assert.equal(job.error, 'could not start the process (ENOENT)');
      assert.equal((await analyze(port, 'examples/textstats')).status, 202);
    });
  });
});

describe('failures', () => {
  test('stderr is tailed to 2000 chars with the repo root and home directory replaced', async () => {
    await withApi({}, async ({ port, runner }) => {
      const r = await analyze(port, 'examples/textstats');
      const child = await nextChild(runner);
      const noise = 'x'.repeat(5000);
      await child.exit(1, { stdout: '', stderr: `${noise}\nFile "${repoRoot}/agent_team/analyst.py", line 3\nsee ${homedir()}/.cache/thing\n{"error": "RuntimeError: nope", "proposal_id": "prop-fail"}\n` });
      const job = await untilStatus(port, r.json.jobId, 'failed');
      assert.ok(job.error.length <= 2000);
      assert.ok(job.error.includes('<project>/agent_team/analyst.py'));
      assert.ok(job.error.includes('~/.cache/thing'));
      assert.ok(!job.error.includes(repoRoot));
      assert.ok(!job.error.includes(homedir()));
      assert.ok(job.error.endsWith('"proposal_id": "prop-fail"}'));
      assert.equal(job.proposalId, 'prop-fail');
      assert.equal(job.result, null);
      assert.equal(job.phase, 'failed');
    });
  });
  test('empty stderr still explains the exit; garbage stdout on exit 0 fails', async () => {
    await withApi({}, async ({ port, runner }) => {
      const a = await analyze(port, 'examples/textstats');
      await (await nextChild(runner, 1)).exit(2);
      assert.equal((await untilStatus(port, a.json.jobId, 'failed')).error, 'process exited with 2');
      const b = await analyze(port, 'examples/textstats');
      await (await nextChild(runner, 2)).exit(0, { stdout: 'not json' });
      assert.equal((await untilStatus(port, b.json.jobId, 'failed')).error, 'output was not a JSON object');
    });
  });
  test('result strings are path-sanitized too', async () => {
    await withApi({}, async ({ port, runner }) => {
      const a = await analyze(port, 'examples/textstats');
      await (await nextChild(runner)).exit(0, { stdout: JSON.stringify({ note: `${repoRoot}/artifacts/runs/x`, nested: [`${homedir()}/y`] }) });
      const job = await untilStatus(port, a.json.jobId, 'done');
      assert.deepEqual(job.result, { note: '<project>/artifacts/runs/x', nested: ['~/y'] });
    });
  });
  test('approve whose run ends short of verified_done (exit 1) is still done, with the summary', async () => {
    await withApi({}, async ({ port, runner }) => {
      const r = await json(port, '/api/analyses/prop-abc/approve', { confirmSpend: true });
      const summary = { run_id: 'r1', state: 'rounds_exhausted', ledger_verified: true };
      await (await nextChild(runner)).exit(1, { stdout: JSON.stringify(summary), stderr: '{"status": "prepared"}' });
      const job = await untilStatus(port, r.json.jobId, 'done');
      assert.deepEqual(job.result, summary);
      assert.equal(job.error, null);
    });
  });
  test('approve whose prepare failed (nothing on stdout) is failed', async () => {
    await withApi({}, async ({ port, runner }) => {
      const r = await json(port, '/api/analyses/prop-abc/approve', { confirmSpend: true });
      await (await nextChild(runner)).exit(1, { stderr: '{"status": "prepare_failed"}\nPrepare failed; the error was recorded.' });
      const job = await untilStatus(port, r.json.jobId, 'failed');
      assert.match(job.error, /Prepare failed/);
    });
  });
});

describe('cancel and timeouts', () => {
  test('SIGINT, then SIGTERM after the grace period, then SIGKILL', async () => {
    await withApi({ jobOptions: { graceMs: 60 } }, async ({ port, runner }) => {
      const r = await analyze(port, 'examples/textstats');
      const child = await nextChild(runner);
      await untilStatus(port, r.json.jobId, 'running');
      const cancel = await raw(port, `/api/jobs/${r.json.jobId}/cancel`, { method: 'POST' });
      assert.equal(cancel.status, 200);
      assert.equal(cancel.json.status, 'running');
      assert.deepEqual(child.signals, ['SIGINT']);
      await raw(port, `/api/jobs/${r.json.jobId}/cancel`, { method: 'POST' }); // repeating does not restart the sequence
      assert.deepEqual(child.signals, ['SIGINT']);
      await until(() => child.signals.length === 2, 'SIGTERM');
      assert.deepEqual(child.signals, ['SIGINT', 'SIGTERM']);
      await until(() => child.signals.length === 3, 'SIGKILL');
      assert.deepEqual(child.signals, ['SIGINT', 'SIGTERM', 'SIGKILL']);
      assert.equal((await getJob(port, r.json.jobId)).status, 'running'); // only the process exit ends a job
      await child.exit(null, { signal: 'SIGKILL', stderr: 'Traceback...' });
      const job = await untilStatus(port, r.json.jobId, 'cancelled');
      assert.equal(job.phase, 'cancelled');
      assert.equal((await analyze(port, 'examples/textstats')).status, 202);
    });
  });
  test('a child that exits after SIGINT stops the escalation', async () => {
    await withApi({ jobOptions: { graceMs: 40 } }, async ({ port, runner }) => {
      const r = await json(port, '/api/analyses/prop-abc/approve', { confirmSpend: true });
      const child = await nextChild(runner);
      await raw(port, `/api/jobs/${r.json.jobId}/cancel`, { method: 'POST' });
      await child.exit(130, { stderr: 'KeyboardInterrupt' });
      await untilStatus(port, r.json.jobId, 'cancelled');
      await new Promise((resolve) => setTimeout(resolve, 150));
      assert.deepEqual(child.signals, ['SIGINT']);
    });
  });
  test('cancel while still queued signals the child as soon as it exists', async () => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    await withApi({ runner: fakeRunner({ gate }) }, async ({ port, runner }) => {
      const r = await analyze(port, 'examples/textstats');
      assert.equal((await raw(port, `/api/jobs/${r.json.jobId}/cancel`, { method: 'POST' })).json.status, 'queued');
      release();
      const child = await nextChild(runner);
      await until(() => child.signals.length === 1, 'SIGINT');
      assert.deepEqual(child.signals, ['SIGINT']);
      await child.exit(130);
      await untilStatus(port, r.json.jobId, 'cancelled');
    });
  });
  test('cancel of unknown or finished jobs sends nothing', async () => {
    await withApi({}, async ({ port, runner }) => {
      assert.equal((await raw(port, '/api/jobs/job-000000000000/cancel', { method: 'POST' })).status, 404);
      assert.equal((await raw(port, '/api/jobs/zzz/cancel', { method: 'POST' })).status, 404);
      const r = await analyze(port, 'examples/textstats');
      const child = await nextChild(runner);
      await child.exit(0, { stdout: '{}' });
      await untilStatus(port, r.json.jobId, 'done');
      const cancel = await raw(port, `/api/jobs/${r.json.jobId}/cancel`, { method: 'POST' });
      assert.equal(cancel.json.status, 'done');
      assert.deepEqual(child.signals, []);
    });
  });
  test('timeout runs the cancel sequence and fails the job', async () => {
    await withApi({ jobOptions: { timeouts: { analyze: 40 }, graceMs: 30 } }, async ({ port, runner }) => {
      const r = await analyze(port, 'examples/textstats');
      const child = await nextChild(runner);
      await until(() => child.signals.length === 1, 'SIGINT after the timeout');
      assert.deepEqual(child.signals, ['SIGINT']);
      await child.exit(130, { stderr: 'KeyboardInterrupt' });
      const job = await untilStatus(port, r.json.jobId, 'failed');
      assert.match(job.error, /^timed out after /);
      assert.match(job.error, /KeyboardInterrupt/);
    });
  });
  test('default timeouts: analyze 10 minutes, approve 50', async () => {
    const real = globalThis.setTimeout;
    const delays = [];
    globalThis.setTimeout = (fn, ms, ...rest) => { delays.push(ms); return real(fn, ms, ...rest); };
    try {
      await withApi({}, async ({ port, runner }) => {
        const a = await analyze(port, 'examples/textstats');
        await (await nextChild(runner, 1)).exit(0, { stdout: '{}' });
        await untilStatus(port, a.json.jobId, 'done');
        const b = await json(port, '/api/analyses/prop-abc/approve', { confirmSpend: true });
        await (await nextChild(runner, 2)).exit(0, { stdout: '{"state":"verified_done"}' });
        await untilStatus(port, b.json.jobId, 'done');
      });
    } finally { globalThis.setTimeout = real; }
    assert.ok(delays.includes(10 * 60_000));
    assert.ok(delays.includes(50 * 60_000));
  });
});

describe('approve discovers the run directory', () => {
  test('runId appears once the ledger for this session exists', async () => {
    const runsRoot = makeRunsRoot();
    addRun(runsRoot, 'older-run', baseEvents({ session: 'dev-other' })); // an unrelated, older-session run
    const runner = fakeRunner();
    const api = await startApi(runsRoot, { runner, jobOptions: { pollMs: 20 } });
    try {
      const r = await json(api.port, '/api/analyses/prop-abc/approve', { confirmSpend: true });
      const { sessionId } = r.json;
      const child = await nextChild(runner);
      const running = await untilStatus(api.port, r.json.jobId, 'running');
      assert.equal(running.phase, 'preparing sandbox');
      assert.equal(running.runId, null);
      assert.equal(running.sessionId, sessionId);
      await new Promise((resolve) => setTimeout(resolve, 80));
      assert.equal((await getJob(api.port, r.json.jobId)).runId, null);
      // The CLI names the directory <session>-<stamp>; a decoy with another session id must not match.
      writeLedger(runsRoot, 'decoy-20261017T000000Z', baseEvents({ session: 'ui-other', terminal: false }));
      writeLedger(runsRoot, `${sessionId}-20261017T101010Z`, baseEvents({ session: sessionId, terminal: false }).slice(0, 3));
      const found = await until(async () => { const job = await getJob(api.port, r.json.jobId); return job.runId ? job : null; }, 'runId');
      assert.equal(found.runId, `${sessionId}-20261017T101010Z`);
      assert.equal(found.phase, 'running agents');
      assert.equal(found.status, 'running');
      // The id is directly usable with the existing live tail.
      const live = await raw(api.port, `/api/runs/${found.runId}/result`);
      assert.notEqual(live.status, 404);
      await child.exit(0, { stdout: JSON.stringify({ run_id: 'r', state: 'verified_done' }) });
      const done = await untilStatus(api.port, r.json.jobId, 'done');
      assert.equal(done.runId, `${sessionId}-20261017T101010Z`);
      assert.equal(done.result.state, 'verified_done');
    } finally { await api.close(); }
  });

  test('a run that finishes between polls is still found when the process exits', async () => {
    const runsRoot = makeRunsRoot();
    const runner = fakeRunner();
    const api = await startApi(runsRoot, { runner, jobOptions: { pollMs: 60_000 } });
    try {
      const r = await json(api.port, '/api/analyses/prop-abc/approve', { confirmSpend: true });
      const child = await nextChild(runner);
      writeLedger(runsRoot, 'whatever-name', baseEvents({ session: r.json.sessionId }));
      await child.exit(0, { stdout: '{"state":"rounds_exhausted"}' });
      const done = await untilStatus(api.port, r.json.jobId, 'done');
      assert.equal(done.runId, 'whatever-name');
    } finally { await api.close(); }
  });

  test('runs that predate the job are never matched', async () => {
    const runsRoot = makeRunsRoot();
    const runner = fakeRunner();
    const api = await startApi(runsRoot, { runner, jobOptions: { pollMs: 20 } });
    try {
      const r = await json(api.port, '/api/analyses/prop-abc/approve', { confirmSpend: true });
      const child = await nextChild(runner);
      const stale = writeLedger(runsRoot, 'stale-dir', baseEvents({ session: r.json.sessionId, terminal: false }));
      const old = new Date(Date.now() - 3600_000);
      utimesSync(dirname(stale), old, old);
      await new Promise((resolve) => setTimeout(resolve, 100));
      await child.exit(1, { stderr: 'failed before the run started' });
      const job = await untilStatus(api.port, r.json.jobId, 'failed');
      assert.equal(job.runId, null);
    } finally { await api.close(); }
  });
});

// ---- autonomous jobs: `python -m agent_team auto` streams NFW_PROGRESS lines on stderr and ends with one JSON on stdout
const startAuto = (port, repoUrl = 'examples/textstats', prompt = 'make it faster', extra = {}, headers = {}) => json(port, '/api/auto', { repoUrl, prompt, confirmSpend: true, ...extra }, headers);
const progress = (step, status, extra = {}) => `NFW_PROGRESS ${JSON.stringify({
  ts: '2026-10-17T10:00:00Z', step, attempt: null, status, message: `${step} ${status}`, proposal_id: null, session_id: null, run_dir: null, run_id: null, ...extra,
})}\n`;
const AUTO_RESULT = {
  status: 'verified_done', proposal_id: 'prop-auto', assumptions: ['inputs are ASCII'], attempts: [],
  runs: [{ session_id: 'x-p1', run_dir: 'artifacts/runs/x', run_id: 'r1', state: 'verified_done', median_speedup: 3.1 }],
  final: { run_dir: 'artifacts/runs/x', state: 'verified_done', ledger_final_sha256: 'a'.repeat(64) }, report: 'done', total_cost_usd: 1.5,
};
const autoRunDir = (prefix, n, extra = '') => `${prefix}-p${n}-r1-2026101710${String(n).padStart(2, '0')}00Z${extra}`;

describe('autonomous job (POST /api/auto)', () => {
  test('202 with jobId and sessionPrefix; argv is fixed and keeps the prompt one value', async () => {
    await withApi({}, async ({ port, runner }) => {
      const prompt = '--help $(touch /tmp/x) `id` && "q"\nline2 --session-prefix=evil';
      const r = await startAuto(port, 'https://github.com/octo/repo@main', prompt);
      assert.equal(r.status, 202);
      assert.deepEqual(Object.keys(r.json).sort(), ['jobId', 'sessionPrefix']);
      assert.match(r.json.jobId, /^job-[0-9a-f]{12}$/);
      assert.match(r.json.sessionPrefix, /^ui-auto-\d{8}T\d{6}Z-[0-9a-f]{6}$/);
      await nextChild(runner);
      const call = runner.calls[0];
      assert.deepEqual(call.args, ['-m', 'agent_team', 'auto', '--repo=https://github.com/octo/repo@main', `--prompt=${prompt}`, `--session-prefix=${r.json.sessionPrefix}`]);
      assert.equal(call.args.length, 6);
      assert.equal(call.file, defaultPython(repoRoot));
      assert.equal(call.cwd, repoRoot);
      assert.deepEqual(Object.keys(call).sort(), ['args', 'cwd', 'env', 'file']);
      const job = await getJob(port, r.json.jobId);
      assert.equal(job.kind, 'auto');
      assert.equal(job.sessionPrefix, r.json.sessionPrefix);
      assert.deepEqual(job.steps, []);
    });
  });

  test('confirmSpend must be literally true; repo and prompt are validated like analyses', async () => {
    await withApi({}, async ({ port, runner }) => {
      for (const extra of [{ confirmSpend: undefined }, { confirmSpend: false }, { confirmSpend: 'true' }, { confirmSpend: 1 }]) {
        const r = await startAuto(port, 'examples/textstats', 'x', extra);
        assert.equal(r.status, 400);
        assert.match(r.json.error, /^confirm spending/);
      }
      for (const url of ['http://github.com/octo/repo', 'https://github.com/octo/..', '-rf', '', 7, null]) {
        const r = await startAuto(port, url);
        assert.equal(r.status, 400, String(url));
        assert.match(r.json.error, /repoUrl/);
      }
      for (const prompt of ['', '   ', 'x'.repeat(4001), 5, null, 'bad\0nul']) assert.equal((await startAuto(port, 'examples/textstats', prompt)).status, 400);
      assert.equal(runner.calls.length, 0);
      assert.equal((await startAuto(port, 'examples/textstats', 'x'.repeat(4000))).status, 202);
    });
  });

  test('wrong methods, cross-origin and bad Host are refused before anything runs', async () => {
    await withApi({}, async ({ port, runner }) => {
      for (const method of ['GET', 'PUT', 'DELETE']) {
        const r = await raw(port, '/api/auto', { method });
        assert.equal(r.status, 405, method);
        assert.equal(r.headers.allow, 'POST');
      }
      for (const origin of ['http://evil.example', 'null', `http://localhost:${port + 1}`]) assert.equal((await startAuto(port, 'examples/textstats', 'x', {}, { Origin: origin })).status, 403, origin);
      assert.equal((await startAuto(port, 'examples/textstats', 'x', {}, { Host: 'evil.example' })).status, 403);
      assert.equal((await raw(port, '/api/auto', { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: '{}' })).status, 415);
      assert.equal(runner.calls.length, 0);
      assert.equal((await startAuto(port, 'examples/textstats', 'x', {}, { Origin: `http://127.0.0.1:${port}` })).status, 202);
    });
  });

  test('NFW_UI_LAUNCH=0 refuses it', async () => {
    await withApi({ launch: false }, async ({ port, runner }) => {
      assert.equal((await startAuto(port)).status, 403);
      assert.equal(runner.calls.length, 0);
    });
  });

  test('single-job lock in both directions, released when the job ends', async () => {
    await withApi({}, async ({ port, runner }) => {
      const first = await startAuto(port);
      const child = await nextChild(runner);
      for (const r of [await startAuto(port), await analyze(port, 'examples/textstats'), await json(port, '/api/analyses/prop-abc/approve', { confirmSpend: true })]) {
        assert.equal(r.status, 409);
        assert.deepEqual(r.json, { error: 'another job is running' });
      }
      assert.equal(runner.calls.length, 1);
      await child.exit(0, { stdout: JSON.stringify(AUTO_RESULT) });
      await untilStatus(port, first.json.jobId, 'done');
      const second = await analyze(port, 'examples/textstats');
      assert.equal(second.status, 202);
      assert.equal((await startAuto(port)).status, 409); // an analysis now holds the lock
    });
  });

  test('NFW_PROGRESS lines become ordered steps as they stream, split across chunks, ignoring everything else', async () => {
    await withApi({}, async ({ port, runner }) => {
      const r = await startAuto(port);
      const child = await nextChild(runner);
      await untilStatus(port, r.json.jobId, 'running');
      assert.deepEqual((await getJob(port, r.json.jobId)).steps, []);
      const line = progress('analyze', 'started', { message: 'Analyzing', proposal_id: 'prop-auto' });
      child.stderr.write(`plain log line\n${line.slice(0, 20)}`);
      assert.deepEqual((await getJob(port, r.json.jobId)).steps, []); // half a line is not a step yet
      child.stderr.write(`${line.slice(20)}${progress('analyze', 'ok', { message: `read ${repoRoot}/x and ${homedir()}/y` })}`);
      const job = await until(async () => { const j = await getJob(port, r.json.jobId); return j.steps.length === 2 ? j : null; }, 'two steps');
      assert.deepEqual(job.steps.map((s) => [s.step, s.status]), [['analyze', 'started'], ['analyze', 'ok']]);
      assert.deepEqual(job.steps[0], { ts: '2026-10-17T10:00:00Z', step: 'analyze', attempt: null, status: 'started', message: 'Analyzing', proposal_id: 'prop-auto', session_id: null, run_dir: null, run_id: null });
      assert.equal(job.steps[1].message, 'read <project>/x and ~/y');
      assert.equal(job.proposalId, 'prop-auto');
      assert.equal(job.phase, 'analyze');
      child.stderr.write(progress('repair', 'started', { attempt: 1 }) + progress('prepare', 'failed', { attempt: 1, run_dir: `${repoRoot}/artifacts/runs/x` }));
      const later = await until(async () => { const j = await getJob(port, r.json.jobId); return j.steps.length === 4 ? j : null; }, 'four steps');
      assert.deepEqual(later.steps.map((s) => [s.step, s.attempt, s.status]), [['analyze', null, 'started'], ['analyze', null, 'ok'], ['repair', 1, 'started'], ['prepare', 1, 'failed']]);
      assert.equal(later.steps[3].run_dir, '<project>/artifacts/runs/x');
      await child.exit(1, { stdout: JSON.stringify({ ...AUTO_RESULT, status: 'gave_up' }), stderr: progress('done', 'failed', { message: 'gave up' }).trimEnd() }); // last line has no newline
      const done = await untilStatus(port, r.json.jobId, 'done');
      assert.equal(done.steps.at(-1).step, 'done');
      assert.equal(done.steps.at(-1).status, 'failed');
    });
  });

  test('malformed progress lines are ignored safely', async () => {
    await withApi({}, async ({ port, runner }) => {
      const r = await startAuto(port);
      const child = await nextChild(runner);
      const bad = [
        'NFW_PROGRESS', 'NFW_PROGRESS ', 'NFW_PROGRESS not json', 'NFW_PROGRESS {"step":', 'NFW_PROGRESS null', 'NFW_PROGRESS []', 'NFW_PROGRESS "analyze"',
        'NFW_PROGRESS {"step":"nope","status":"ok"}', 'NFW_PROGRESS {"step":"analyze","status":"weird"}', 'NFW_PROGRESS {"step":"analyze"}',
        'NFW_PROGRESS {"step":"__proto__","status":"ok"}', ' NFW_PROGRESS {"step":"analyze","status":"ok"}', `NFW_PROGRESS {"step":"analyze","status":"ok","message":"${'x'.repeat(20000)}"}`,
      ];
      child.stderr.write(`${bad.join('\n')}\n`);
      child.stderr.write(progress('analyze', 'ok', { attempt: 'one', message: 42, proposal_id: '../../etc', run_id: 'a b', session_id: {}, ts: 7 }));
      child.stderr.write(`NFW_PROGRESS {"step":"run","status":"started","message":"${'y'.repeat(900)}","attempt":2}\n`);
      const job = await until(async () => { const j = await getJob(port, r.json.jobId); return j.steps.length === 2 ? j : null; }, 'two valid steps');
      assert.deepEqual(job.steps[0], { ts: null, step: 'analyze', attempt: null, status: 'ok', message: '', proposal_id: null, session_id: null, run_dir: null, run_id: null });
      assert.equal(job.steps[1].step, 'run');
      assert.equal(job.steps[1].attempt, 2);
      assert.equal(job.steps[1].message.length, 600);
      assert.equal(job.proposalId, null);
      child.stderr.write('x'.repeat(40_000)); // an endless line never becomes a step and does not grow without bound
      child.stderr.write(`\n${progress('done', 'ok')}`);
      const after = await until(async () => { const j = await getJob(port, r.json.jobId); return j.steps.length === 3 ? j : null; }, 'the step after the long line');
      assert.equal(after.steps[2].step, 'done');
    });
  });

  test('only the last 200 steps are kept', async () => {
    await withApi({}, async ({ port, runner }) => {
      const r = await startAuto(port);
      const child = await nextChild(runner);
      child.stderr.write(Array.from({ length: 250 }, (_, i) => progress('run', 'started', { attempt: i, message: `m${i}` })).join(''));
      const job = await until(async () => { const j = await getJob(port, r.json.jobId); return j.steps.at(-1)?.attempt === 249 ? j : null; }, '250 lines');
      assert.equal(job.steps.length, 200);
      assert.equal(job.steps[0].attempt, 50);
    });
  });

  test('progress lines on the other job kinds are not parsed', async () => {
    await withApi({}, async ({ port, runner }) => {
      const r = await analyze(port, 'examples/textstats');
      const child = await nextChild(runner);
      await child.exit(1, { stderr: progress('analyze', 'ok') });
      assert.deepEqual((await untilStatus(port, r.json.jobId, 'failed')).steps, []);
    });
  });

  test('status mapping: exit 0, 3 and 1-with-JSON are done; no JSON or another code is failed', async () => {
    await withApi({}, async ({ port, runner }) => {
      let count = 0;
      const run = async (code, stdout, stderr = '') => {
        const r = await startAuto(port);
        count += 1;
        await (await nextChild(runner, count)).exit(code, { stdout, stderr });
        return untilStatus(port, r.json.jobId, 'done', 'failed');
      };
      for (const [code, status] of [[0, 'verified_done'], [3, 'not_feasible'], [1, 'gave_up']]) {
        const job = await run(code, JSON.stringify({ ...AUTO_RESULT, status }));
        assert.equal(job.status, 'done', `exit ${code}`);
        assert.equal(job.result.status, status);
        assert.equal(job.error, null);
        assert.equal(job.phase, 'done');
      }
      const none = await run(1, '', 'Traceback: boom');
      assert.equal(none.status, 'failed');
      assert.equal(none.result, null);
      assert.match(none.error, /boom/);
      assert.equal((await run(1, 'not json')).status, 'failed');
      assert.equal((await run(0, 'not json')).error, 'output was not a JSON object');
      assert.equal((await run(2, JSON.stringify(AUTO_RESULT))).status, 'failed'); // an unexpected code never counts as an answer
      assert.equal((await run(null, JSON.stringify(AUTO_RESULT))).status, 'failed');
    });
  });

  test('the result is path-sanitized and the final run_dir is used when no run was discovered', async () => {
    await withApi({}, async ({ port, runner }) => {
      const r = await startAuto(port);
      await (await nextChild(runner)).exit(0, { stdout: JSON.stringify({ ...AUTO_RESULT, report: `see ${repoRoot}/artifacts/runs/x and ${homedir()}/z`, final: { run_dir: `${repoRoot}/artifacts/runs/final-dir`, state: 'verified_done' } }) });
      const job = await untilStatus(port, r.json.jobId, 'done');
      assert.equal(job.result.report, 'see <project>/artifacts/runs/x and ~/z');
      assert.equal(job.currentRunId, 'final-dir');
      assert.equal(job.runId, 'final-dir');
    });
  });

  test('cancel: SIGINT escalation, and exit 1 after a cancel is cancelled (not gave_up); exit 0 still wins', async () => {
    await withApi({ jobOptions: { graceMs: 40 } }, async ({ port, runner }) => {
      const a = await startAuto(port);
      const first = await nextChild(runner, 1);
      await untilStatus(port, a.json.jobId, 'running');
      first.stderr.write(progress('run', 'started', { attempt: 1 }));
      const cancel = await raw(port, `/api/jobs/${a.json.jobId}/cancel`, { method: 'POST' });
      assert.equal(cancel.status, 200);
      assert.deepEqual(first.signals, ['SIGINT']);
      await until(() => first.signals.length === 3, 'SIGTERM and SIGKILL');
      assert.deepEqual(first.signals, ['SIGINT', 'SIGTERM', 'SIGKILL']);
      await first.exit(1, { stdout: JSON.stringify({ ...AUTO_RESULT, status: 'gave_up' }), stderr: 'KeyboardInterrupt' });
      const job = await untilStatus(port, a.json.jobId, 'cancelled');
      assert.equal(job.result, null);
      assert.equal(job.steps.length, 1);

      const b = await startAuto(port);
      const second = await nextChild(runner, 2);
      await raw(port, `/api/jobs/${b.json.jobId}/cancel`, { method: 'POST' });
      await second.exit(0, { stdout: JSON.stringify(AUTO_RESULT) }); // it finished before the signal landed
      assert.equal((await untilStatus(port, b.json.jobId, 'done', 'cancelled')).status, 'done');
    });
  });

  test('timeout is 120 minutes by default and a timed-out job fails even with a gave_up JSON', async () => {
    const real = globalThis.setTimeout;
    const delays = [];
    globalThis.setTimeout = (fn, ms, ...rest) => { delays.push(ms); return real(fn, ms, ...rest); };
    try {
      await withApi({}, async ({ port, runner }) => {
        const r = await startAuto(port);
        await (await nextChild(runner)).exit(0, { stdout: JSON.stringify(AUTO_RESULT) });
        await untilStatus(port, r.json.jobId, 'done');
      });
    } finally { globalThis.setTimeout = real; }
    assert.ok(delays.includes(120 * 60_000));
    await withApi({ jobOptions: { timeouts: { auto: 40 }, graceMs: 30 } }, async ({ port, runner }) => {
      const r = await startAuto(port);
      const child = await nextChild(runner);
      await until(() => child.signals.length === 1, 'SIGINT after the timeout');
      await child.exit(1, { stdout: JSON.stringify({ ...AUTO_RESULT, status: 'gave_up' }), stderr: 'KeyboardInterrupt' });
      const job = await untilStatus(port, r.json.jobId, 'failed');
      assert.match(job.error, /^timed out after /);
    });
  });
});

describe('autonomous job discovers its runs', () => {
  test('any ledger whose session starts with <prefix>-p counts; a second run replaces the first', async () => {
    const runsRoot = makeRunsRoot();
    addRun(runsRoot, 'older-run', baseEvents({ session: 'dev-other' }));
    const runner = fakeRunner();
    const api = await startApi(runsRoot, { runner, jobOptions: { pollMs: 20 } });
    try {
      const r = await startAuto(api.port);
      const { sessionPrefix } = r.json;
      const child = await nextChild(runner);
      await untilStatus(api.port, r.json.jobId, 'running');
      await new Promise((resolve) => setTimeout(resolve, 80));
      assert.equal((await getJob(api.port, r.json.jobId)).currentRunId, null);
      // decoys: other prefixes, the bare prefix, and a session that merely contains the prefix
      writeLedger(runsRoot, 'decoy-a', baseEvents({ session: 'ui-auto-other-p1', terminal: false }));
      writeLedger(runsRoot, 'decoy-b', baseEvents({ session: sessionPrefix, terminal: false }));
      writeLedger(runsRoot, 'decoy-c', baseEvents({ session: `x${sessionPrefix}-p1`, terminal: false }));
      writeLedger(runsRoot, 'decoy-d', baseEvents({ session: `${sessionPrefix}p1`, terminal: false }));
      await new Promise((resolve) => setTimeout(resolve, 100));
      assert.equal((await getJob(api.port, r.json.jobId)).currentRunId, null);

      const run1 = autoRunDir(sessionPrefix, 1);
      writeLedger(runsRoot, run1, baseEvents({ session: `${sessionPrefix}-p1`, terminal: false }).slice(0, 3));
      const first = await until(async () => { const j = await getJob(api.port, r.json.jobId); return j.currentRunId ? j : null; }, 'run 1');
      assert.equal(first.currentRunId, run1);
      assert.equal(first.runId, run1);
      assert.equal((await raw(api.port, `/api/runs/${first.currentRunId}/result`)).status !== 404, true);

      const run2 = autoRunDir(sessionPrefix, 2);
      writeLedger(runsRoot, run2, baseEvents({ session: `${sessionPrefix}-p2`, terminal: false }).slice(0, 3));
      const second = await until(async () => { const j = await getJob(api.port, r.json.jobId); return j.currentRunId === run2 ? j : null; }, 'run 2 replacing run 1');
      assert.equal(second.runId, run2);
      await new Promise((resolve) => setTimeout(resolve, 80));
      assert.equal((await getJob(api.port, r.json.jobId)).currentRunId, run2); // older directories never come back
      await child.exit(0, { stdout: JSON.stringify(AUTO_RESULT) });
      const done = await untilStatus(api.port, r.json.jobId, 'done');
      assert.equal(done.currentRunId, run2);
    } finally { await api.close(); }
  });

  test('two runs that appear between polls: the newest by run_started time wins on exit', async () => {
    const runsRoot = makeRunsRoot();
    const runner = fakeRunner();
    const api = await startApi(runsRoot, { runner, jobOptions: { pollMs: 60_000 } });
    try {
      const r = await startAuto(api.port);
      const { sessionPrefix } = r.json;
      const child = await nextChild(runner);
      const later = baseEvents({ session: `${sessionPrefix}-p2`, terminal: false }).map((event) => ({ ...event, utc: event.utc.replace('T10:', 'T11:') }));
      writeLedger(runsRoot, 'aaa-newest-name-first', later); // sorts first by name but started later
      writeLedger(runsRoot, 'zzz-older-run', baseEvents({ session: `${sessionPrefix}-p1`, terminal: false }));
      await child.exit(1, { stdout: JSON.stringify({ ...AUTO_RESULT, status: 'gave_up' }) });
      const done = await untilStatus(api.port, r.json.jobId, 'done');
      assert.equal(done.currentRunId, 'aaa-newest-name-first');
    } finally { await api.close(); }
  });

  test('runs that predate the job are never matched', async () => {
    const runsRoot = makeRunsRoot();
    const runner = fakeRunner();
    const api = await startApi(runsRoot, { runner, jobOptions: { pollMs: 20 } });
    try {
      const r = await startAuto(api.port);
      const child = await nextChild(runner);
      const stale = writeLedger(runsRoot, 'stale-dir', baseEvents({ session: `${r.json.sessionPrefix}-p1`, terminal: false }));
      const old = new Date(Date.now() - 3600_000);
      utimesSync(dirname(stale), old, old);
      await new Promise((resolve) => setTimeout(resolve, 100));
      await child.exit(1, { stderr: 'failed before any run' });
      const job = await untilStatus(api.port, r.json.jobId, 'failed');
      assert.equal(job.currentRunId, null);
    } finally { await api.close(); }
  });
});

describe('launch disabled', () => {
  test('NFW_UI_LAUNCH=0 refuses every launch POST and reports launch:false', async () => {
    const previous = process.env.NFW_UI_LAUNCH;
    process.env.NFW_UI_LAUNCH = '0';
    try {
      await withApi({}, async ({ port, runner }) => {
        const health = (await raw(port, '/api/health')).json;
        assert.equal(health.launch, false);
        assert.equal(health.mode, 'replay');
        assert.equal(health.ok, true);
        assert.equal((await analyze(port, 'examples/textstats')).status, 403);
        assert.equal((await json(port, '/api/analyses/prop-abc/answer', { answer: 'x', confirmSpend: true })).status, 403);
        assert.equal((await json(port, '/api/analyses/prop-abc/approve', { confirmSpend: true })).status, 403);
        assert.equal((await raw(port, '/api/jobs/job-000000000000/cancel', { method: 'POST' })).status, 403);
        assert.equal(runner.calls.length, 0);
        assert.deepEqual((await raw(port, '/api/jobs')).json, { jobs: [] });
      });
    } finally {
      if (previous === undefined) delete process.env.NFW_UI_LAUNCH; else process.env.NFW_UI_LAUNCH = previous;
    }
  });
  test('the option works without the env, and the default is enabled', async () => {
    await withApi({ launch: false }, async ({ port }) => {
      assert.equal((await raw(port, '/api/health')).json.launch, false);
      assert.equal((await analyze(port, 'examples/textstats')).status, 403);
    });
    assert.equal(typeof createApi, 'function');
  });
});

describe('same-origin and Host checks on launch POSTs', () => {
  test('cross-origin and bad Host are refused before anything runs', async () => {
    await withApi({}, async ({ port, runner }) => {
      const body = { repoUrl: 'examples/textstats', prompt: 'x', confirmSpend: true };
      const paths = [['/api/analyses', body], ['/api/analyses/prop-abc/answer', { answer: 'x', confirmSpend: true }], ['/api/analyses/prop-abc/approve', { confirmSpend: true }]];
      for (const [path, payload] of paths) {
        for (const origin of ['http://evil.example', 'null', `http://localhost:${port + 1}`, `https://127.0.0.1:${port}`]) {
          assert.equal((await json(port, path, payload, { Origin: origin })).status, 403, `${path} ${origin}`);
        }
        assert.equal((await json(port, path, payload, { Host: 'evil.example' })).status, 403, `${path} Host`);
        assert.equal((await json(port, path, payload, { Host: `127.0.0.1:${port + 1}` })).status, 403, `${path} Host port`);
      }
      assert.equal((await raw(port, '/api/jobs/job-000000000000/cancel', { method: 'POST', headers: { Origin: 'http://evil.example' } })).status, 403);
      assert.equal((await raw(port, '/api/jobs/job-000000000000/cancel', { method: 'POST', headers: { Host: 'evil.example' } })).status, 403);
      assert.equal((await raw(port, '/api/jobs', { headers: { Host: 'evil.example' } })).status, 403);
      assert.equal(runner.calls.length, 0);
    });
  });
  test('same-origin forms are accepted', async () => {
    await withApi({}, async ({ port, runner }) => {
      const first = await analyze(port, 'examples/textstats', 'x', {}); // no Origin header (curl style)
      assert.equal(first.status, 202);
      await (await nextChild(runner, 1)).exit(0, { stdout: '{}' });
      await untilStatus(port, first.json.jobId, 'done');
      for (const [index, origin] of [`http://127.0.0.1:${port}`, `http://localhost:${port}`].entries()) {
        const r = await json(port, '/api/analyses', { repoUrl: 'examples/textstats', prompt: 'x', confirmSpend: true }, { Origin: origin });
        assert.equal(r.status, 202, origin);
        await (await nextChild(runner, index + 2)).exit(0, { stdout: '{}' });
        await untilStatus(port, r.json.jobId, 'done');
      }
    });
  });
});

describe('the default runner is never used by tests', () => {
  test('startApi refuses to spawn when no runner is injected', async () => {
    const runsRoot = makeRunsRoot();
    const api = await startApi(runsRoot);
    try {
      const r = await analyze(api.port, 'examples/textstats');
      assert.equal(r.status, 202);
      const job = await untilStatus(api.port, r.json.jobId, 'failed');
      assert.equal(job.error, 'could not start the process (EREFUSED)');
    } finally { await api.close(); }
  });
});
