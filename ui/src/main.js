// UI controller. Replays recorded runs through the local read-only bridge (adapter.js) and falls
// back to the scripted demo when the API is unreachable. No model calls, source fetching, or sandbox execution.
// Controller-facing types live in contracts.d.ts.
import { codeBytes, proof, phaseNames, phaseNotes } from './demo-data.js';
import { render } from './scenes.js';
import { createAdapter } from './adapter.js';
import { lineDiff, milestone } from './presentation.js';

const adapter = createAdapter();
const STAGE_PHASE = { analyze: 1, workers: 2, verify: 3, race: 4, zoo: 5, result: 6 };
const SPEED_PARAM = { 1: 1, 8: 8, instant: 0 };
const LAUNCH_NOTE = 'Launching from a link is coming next; choose a recorded run.';
const NOTICE = {
  checking: 'Checking for the local backend…',
  up: 'Replay saved evidence. Explore the code and the proof. No new run starts here.',
  down: 'Demo mode · local scenes and scripted chat. Backend disconnected. Links are never fetched.',
};
const BADGE = { checking: 'Connecting…', up: 'Recorded replay', down: 'Example mode' };

const app = document.querySelector('[data-app="sidecar"]');
const input = app.querySelector('textarea');
const source = app.querySelector('input');
const error = app.querySelector('.source-error');
const note = app.querySelector('.source-note');
const conversation = app.querySelector('.conversation-scroll');
const messages = app.querySelector('.chat-extra');
const view = {
  element: app,
  phase: 0,
  sourceOpen: true,
  playing: false,
  timer: null,
  api: 'checking',
  runs: [],
  runsError: '',
  run: null,
  speed: 8,
  follow: true,
  artifactUrl: adapter.artifactUrl,
};
let chatController = null;
let frame = 0;

function stop() {
  clearTimeout(view.timer);
  view.timer = null;
  view.playing = false;
}

function go(phase, animate = false) {
  view.phase = Math.max(0, Math.min(phaseNames.length - 1, phase));
  if (view.phase > 0) view.sourceOpen = false;
  cancelAnimationFrame(frame);
  frame = 0;
  render(view, animate);
}

function refresh() {
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    render(view, false);
  });
}

function applyMode() {
  app.querySelector('.demo-badge').textContent = BADGE[view.api];
  app.querySelector('.demo-notice').textContent = NOTICE[view.api];
  app.querySelector('.chat-hint').textContent = view.api === 'up'
    ? 'Ask while the replay runs · answers use recorded evidence'
    : 'Local scripted replies · Enter to send · Shift+Enter for a new line';
  note.textContent = LAUNCH_NOTE;
  note.hidden = view.api !== 'up';
}

// ---- demo (scripted) ---------------------------------------------------------------------
function play() {
  stop();
  view.playing = true;
  go(1, true);
  function advance() {
    if (!view.playing) return;
    go(view.phase + 1, true);
    if (view.phase === phaseNames.length - 1) {
      stop();
      render(view, false);
      return;
    }
    view.timer = setTimeout(advance, view.phase === 4 ? 4700 : 3300);
  }
  view.timer = setTimeout(advance, 2500);
}

function addMessage(role, text, speakerText = '✳ No Free Wins · demo', extra = '') {
  const article = document.createElement('article');
  article.className = `message ${role}${extra ? ` ${extra}` : ''}`;
  if (role === 'agent') {
    const speaker = document.createElement('div');
    speaker.className = 'speaker';
    speaker.textContent = speakerText;
    article.append(speaker);
  }
  const body = document.createElement('p');
  body.className = 'chat-body';
  body.textContent = text;
  article.append(body);
  messages.append(article);
  conversation.scrollTop = conversation.scrollHeight;
  return article;
}

// ---- recorded-run replay -----------------------------------------------------------------
const table = () => Object.create(null);

function newRun(meta) {
  return {
    id: meta.runId,
    meta,
    status: 'streaming',
    controller: new AbortController(),
    seen: new Set(),
    failed: new Set(),
    terminal: null,
    result: null,
    resultState: 'idle',
    milestones: new Set(),
    race: { current: 0, total: 0, playing: false },
    changes: { open: false, state: 'idle' },
    presentation: { candidates: {}, pairTimings: [] },
    error: '',
    data: {
      sessionId: meta.sessionId, models: null, maxRounds: null, deadline: null, label: '', faults: [],
      strategyIds: [], workers: table(), candidates: table(), screens: table(), verdicts: table(), acceptances: [],
    },
  };
}

function applyEvent(run, event) {
  const d = run.data;
  const x = event.detail && typeof event.detail === 'object' ? event.detail : {};
  const text = (v) => (typeof v === 'string' ? v : undefined);
  const worker = (key) => (typeof key === 'string' ? (d.workers[key] ||= { attempts: [] }) : null);
  const candidate = (id) => (typeof id === 'string' ? (d.candidates[id] ||= {}) : null);
  switch (event.kind) {
    case 'run_started':
      d.sessionId = text(x.session_id) || d.sessionId;
      d.models = x.models && typeof x.models === 'object' ? x.models : null;
      d.maxRounds = x.max_rounds;
      d.deadline = x.deadline_seconds;
      break;
    case 'run_label':
      d.label = text(x.label) || d.label;
      break;
    case 'fault_injection':
      if (text(x.label)) d.faults.push(x.label);
      break;
    case 'plan':
      d.strategyIds = Array.isArray(x.strategy_ids) ? x.strategy_ids.map(String) : [];
      break;
    case 'worker_order': {
      const w = worker(x.worker);
      if (w) Object.assign(w, { strategyId: text(x.strategy_id), primary: x.primary, fallback: x.fallback });
      break;
    }
    case 'candidate_created': {
      const w = worker(x.worker);
      const c = candidate(x.candidate_id);
      if (w && c) {
        Object.assign(w, { candidateId: x.candidate_id, sha: text(x.candidate_sha256), attempts: Array.isArray(x.attempts) ? x.attempts : [] });
        delete w.visualAttempt;
        w.strategyId = text(x.strategy_id) || w.strategyId;
        Object.assign(c, { worker: x.worker, sha: w.sha, strategyId: w.strategyId });
      }
      break;
    }
    case 'worker_failed': {
      const w = worker(x.worker);
      if (w) Object.assign(w, { failed: true, attempts: x.attempts || [] });
      break;
    }
    case 'screen_result':
      if (candidate(x.candidate_id)) d.screens[x.candidate_id] = x;
      break;
    case 'verifier_verdict':
      if (candidate(x.candidate_id)) d.verdicts[x.candidate_id] = x;
      break;
    case 'acceptance_result':
      if (candidate(x.candidate_id)) d.acceptances.push(x);
      break;
    case 'terminal':
      run.terminal = x;
      break;
    default:
  }
}

function abortReplay() {
  if (view.run) view.run.controller.abort();
}

function clearReplayMessages() {
  messages.querySelectorAll('.replay-event').forEach((node) => node.remove());
}

function onEvent(run, event) {
  const known = Object.hasOwn(STAGE_PHASE, event.stage);
  run.lastStage = event.stage;
  applyEvent(run, event);
  if (event.kind === 'acceptance_result' && run.resultState === 'idle') fetchResult(run).then(() => { if (view.run === run) refresh(); });
  if (known) {
    run.seen.add(event.stage);
    if (event.status === 'failed') run.failed.add(event.stage);
  }
  const message = milestone(event.stage);
  if (message && !run.milestones.has(event.stage)) {
    run.milestones.add(event.stage);
    addMessage('agent', message, '✳ No Free Wins · replay', 'replay-event');
  }
  if (view.follow && known && view.phase !== STAGE_PHASE[event.stage]) go(STAGE_PHASE[event.stage], true);
  else refresh();
}

function fetchResult(run) {
  run.resultState = 'loading';
  run.resultPromise = adapter.result(run.id).then(
    (result) => { run.result = result; run.resultState = 'ready'; },
    () => { run.resultState = 'error'; },
  );
  return run.resultPromise;
}

async function loadResult(run) {
  // Re-fetch at stream end: a result requested at the acceptance event may predate the terminal state.
  refresh();
  await run.resultPromise?.catch(() => {});
  await fetchResult(run);
  if (view.run !== run) return;
  if (view.follow && view.phase !== 6) go(6, true);
  else go(view.phase, false);
}

async function replay(meta) {
  abortReplay();
  stop();
  chatController?.abort();
  const run = newRun(meta);
  run.presentationPromise = adapter.presentation(meta.runId, run.controller.signal).then(
    data => { run.presentation = data; if (view.run === run) refresh(); },
    () => {},
  );
  view.run = run;
  view.follow = true;
  view.sourceOpen = false;
  error.hidden = true;
  clearReplayMessages();
  app.querySelector('.history .attachment').textContent = `↗ Recorded run · ${meta.label || meta.runId}`;
  app.querySelector('.history .user .chat-body').textContent = `Replay the recorded run ${meta.runId}.`;
  go(1, true);
  try {
    const options = { speed: SPEED_PARAM[view.speed], onEnd: (state) => { run.endState = state; } };
    for await (const event of adapter.events(meta.runId, run.controller.signal, options)) {
      if (view.run !== run) return;
      if (event.kind === 'candidate_created') await showAttempts(run, event);
      if (run.controller.signal.aborted || view.run !== run) return;
      onEvent(run, event);
      if (event.kind === 'acceptance_result') {
        await run.presentationPromise;
        await run.resultPromise;
        await replayPairs(run);
        if (view.follow && view.speed !== 'instant' && !run.controller.signal.aborted) {
          go(5, true);
          await delay(1600, run.controller.signal);
        }
      }
    }
    if (run.controller.signal.aborted) {
      if (view.run === run) run.status = 'stopped';
      refresh();
      return;
    }
    run.status = 'done';
    await loadResult(run);
  } catch (failure) {
    if (view.run !== run || run.controller.signal.aborted) return;
    run.status = 'error';
    run.error = failure instanceof Error ? failure.message : 'unknown error';
    go(view.phase, false);
  }
}

function delay(ms, signal) {
  return new Promise(resolve => {
    if (signal.aborted || ms <= 0) return resolve();
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
    const timer = setTimeout(finish, ms);
    signal.addEventListener('abort', finish, { once: true });
  });
}

async function showAttempts(run, event) {
  const attempts = event.detail?.attempts || [];
  if (attempts.length < 2 || view.speed === 'instant') return;
  const key = event.detail.worker;
  const worker = run.data.workers[key];
  if (!worker) return;
  for (const attempt of attempts) {
    if (run.controller.signal.aborted || view.run !== run) return;
    worker.visualAttempt = attempt;
    if (view.follow) go(2, true); else refresh();
    await delay(view.speed === 1 ? 1000 : 550, run.controller.signal);
  }
}

async function replayPairs(run) {
  const pairs = run.presentation.pairTimings || [];
  if (!run.result?.accepted || !pairs.length || run.race.playing) return;
  run.race.total = pairs.length;
  if (view.speed === 'instant') { run.race.current = pairs.length; refresh(); return; }
  run.race.playing = true;
  for (let i = 0; i < pairs.length; i++) {
    if (run.controller.signal.aborted || view.run !== run) break;
    run.race.current = i + 1;
    run.race.pair = pairs[i];
    run.race.windowMs = view.speed === 1 ? 1000 : 420;
    if (view.follow) go(4, true); else refresh();
    await delay(run.race.windowMs + 60, run.controller.signal);
  }
  run.race.playing = false;
  if (view.run === run) refresh();
}

async function showChanges() {
  const run = view.run;
  if (!run?.result?.accepted) return;
  run.changes.open = !run.changes.open;
  if (run.changes.state === 'idle') {
    run.changes.state = 'loading';
    refresh();
    try {
      const [original, optimized] = await Promise.all([
        adapter.source(run.result.originalArtifactId, run.controller.signal),
        adapter.source(run.result.optimizedArtifactId, run.controller.signal),
      ]);
      run.changes.diff = lineDiff(original, optimized);
      run.changes.state = 'ready';
    } catch { run.changes.state = 'error'; }
  }
  if (view.run === run) refresh();
}

async function loadRuns() {
  try {
    view.runs = await adapter.runs(AbortSignal.timeout(5000));
    view.runsError = '';
  } catch {
    view.runs = [];
    view.runsError = 'Could not load the recorded runs list.';
  }
}

async function connect() {
  try {
    const health = await adapter.health(AbortSignal.timeout(4000));
    if (health?.ok !== true) throw new Error('Backend not ready');
    view.api = 'up';
    await loadRuns();
  } catch {
    view.api = 'down';
  }
  applyMode();
  go(view.phase, false);
}

async function askRun(run, text) {
  chatController?.abort();
  const controller = new AbortController();
  chatController = controller;
  try {
    const reply = await adapter.chat(run.id, text, controller.signal);
    if (view.run === run) addMessage('agent', reply, '✳ No Free Wins · recorded run');
  } catch {
    if (controller.signal.aborted) return;
    addMessage('agent', 'I couldn’t reach the recorded-run chat just now. Nothing about the run changed.', '✳ No Free Wins · recorded run');
  }
}

function chat(text) {
  if (!text.trim()) return;
  addMessage('user', text);
  if (view.run) {
    input.value = '';
    askRun(view.run, text.trim());
    return;
  }
  let answer;
  if (/\bstop\b|\bpause\b/i.test(text)) {
    stop();
    render(view, false);
    answer = 'The local replay is paused. You can still chat or choose a stage.';
  } else if (/change|budget|target|limit|instead/i.test(text)) {
    answer = 'This skeleton only demonstrates the conversation. No live task or budget is changed. The connected version will need a new approved run for changed limits.';
  } else if (/code|download|file/i.test(text)) {
    answer = 'The Result stage has the recorded example’s optimized Python file and proof summary. These are fixtures, unrelated to a link pasted in this preview.';
  } else if (/two|workers|strateg/i.test(text)) {
    answer = 'The proposed scene shows a regex strategy and a token parser in separate sandboxes. This preview runs neither; the current backend evaluates candidates sequentially.';
  } else if (/zoo|animal|cheat/i.test(text)) {
    answer = 'The separate zoo checks defenses with four planted bad implementations and four honest controls. Its recorded pilot is separate from candidate acceptance.';
  } else if (/test|check|proof|verif/i.test(text)) {
    answer = 'The real pipeline checks correctness, gets independent candidate-bound review, then uses 30 matched timing pairs and an audited receipt. This screen is a local replay.';
  } else {
    answer = phaseNotes[view.phase];
  }
  addMessage('agent', answer);
  input.value = '';
}

function startExample() {
  abortReplay();
  view.run = null;
  view.follow = true;
  view.sourceOpen = false;
  error.hidden = true;
  source.removeAttribute('aria-invalid');
  app.querySelector('.history .attachment').textContent = '↗ Example · parser.py';
  app.querySelector('.history .user .chat-body').textContent = 'Make this parser faster. Keep the output exactly the same.';
  play();
}

function submitInput() {
  if (view.run || view.phase !== 0) {
    chat(input.value);
    return;
  }
  if (view.api === 'up') {
    error.textContent = LAUNCH_NOTE;
    error.hidden = false;
    return;
  }
  let url;
  try {
    url = new URL(source.value.trim());
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid URL');
  } catch {
    error.textContent = 'Paste a complete HTTP or HTTPS code link, or choose Play example.';
    error.hidden = false;
    source.setAttribute('aria-invalid', 'true');
    source.focus();
    return;
  }
  if (!input.value.trim()) {
    error.textContent = 'Add an instruction for this preview.';
    error.hidden = false;
    input.focus();
    return;
  }
  error.hidden = true;
  source.removeAttribute('aria-invalid');
  app.querySelector('.history .attachment').textContent = `↗ Preview link · ${url.hostname}`;
  app.querySelector('.history .user .chat-body').textContent = input.value.trim();
  input.value = '';
  view.sourceOpen = false;
  addMessage('agent', 'Link and instruction captured locally. I’ll show the fixed parser example next. Your URL has not been fetched and no optimization is running.');
  play();
}

function download(kind) {
  const blob = new Blob([kind === 'code' ? codeBytes : JSON.stringify(proof, null, 2)], {
    type: kind === 'code' ? 'text/x-python;charset=utf-8' : 'application/json',
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = kind === 'code' ? 'example-optimized.py' : 'example-proof-summary.json';
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

app.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button || !app.contains(button)) return;
  if (button.hasAttribute('data-phase')) {
    if (view.run) view.follow = false;
    else stop();
    go(Number(button.dataset.phase), true);
  } else if (button.hasAttribute('data-run')) {
    const meta = view.runs.find((item) => item.runId === button.dataset.run);
    if (meta) replay(meta);
  } else if (button.hasAttribute('data-speed')) {
    view.speed = button.dataset.speed === 'instant' ? 'instant' : Number(button.dataset.speed);
    if (view.run?.status === 'streaming') replay(view.run.meta);
    else go(view.phase, false);
  } else if (button.hasAttribute('data-replay')) {
    if (view.run.status === 'streaming') {
      view.run.controller.abort();
      view.run.status = 'stopped';
      go(view.phase, false);
    } else replay(view.run.meta);
  } else if (button.hasAttribute('data-play')) {
    if (view.playing) {
      stop();
      render(view, false);
    } else startExample();
  } else if (button.hasAttribute('data-example')) {
    startExample();
  } else if (button.hasAttribute('data-changes')) {
    showChanges();
  } else if (button.hasAttribute('data-pairs')) {
    const run = view.run;
    if (run) { view.follow = true; run.race.current = 0; replayPairs(run); }
  } else if (button.hasAttribute('data-proof')) {
    const owner = view.run || view;
    owner.proofOpen = !owner.proofOpen;
    render(view, false);
  } else if (button.hasAttribute('data-zoo')) {
    go(5, true);
  } else if (button.hasAttribute('data-follow')) {
    view.follow = true;
    go(STAGE_PHASE[view.run.lastStage] || view.phase, true);
  } else if (button.hasAttribute('data-question')) {
    chat(button.dataset.question);
  } else if (button.hasAttribute('data-download')) {
    download(button.dataset.download);
  } else if (button.classList.contains('paste-link')) {
    stop();
    view.sourceOpen = true;
    go(0);
    source.focus();
  } else if (button.classList.contains('new-run')) {
    stop();
    abortReplay();
    chatController?.abort();
    view.run = null;
    view.follow = true;
    view.sourceOpen = true;
    messages.replaceChildren();
    source.value = '';
    input.value = '';
    error.hidden = true;
    source.removeAttribute('aria-invalid');
    go(0);
    if (view.api === 'up') loadRuns().then(() => { if (!view.run && view.phase === 0) go(0); });
    source.focus();
  }
});

app.querySelector('.chat-form').addEventListener('submit', (event) => {
  event.preventDefault();
  submitInput();
});
input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submitInput();
  }
});
window.addEventListener('pagehide', () => {
  stop();
  abortReplay();
});
applyMode();
render(view, false);
connect();
