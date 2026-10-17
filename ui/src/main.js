// UI controller. Replays recorded runs through the local bridge (adapter.js), falls back to the scripted
// demo when the API is unreachable and, when the bridge reports launch enabled, drives the analyst flow:
// analyze -> (answer)* -> approve -> live run. The browser never fetches the repo, calls a model or runs
// repo code; every spend (analyze, answer, approve) follows an explicit user confirmation.
// Controller-facing types live in contracts.d.ts.
import { codeBytes, proof, phaseNames, phaseNotes } from './demo-data.js';
import { render, repoShort } from './scenes.js';
import { createAdapter, repoUrlProblem, isJobActive, ApiError } from './adapter.js';
import { lineDiff, milestone } from './presentation.js';

const adapter = createAdapter();
const STAGE_PHASE = { analyze: 1, workers: 2, verify: 3, race: 4, zoo: 5, result: 6 };
const SPEED_PARAM = { 1: 1, 8: 8, instant: 0 };
const LAUNCH_NOTE = 'Launching from a link is coming next; choose a recorded run.';
const SOURCE_NOTE = 'Public GitHub repos only: https://github.com/owner/name (optionally .git or @ref), or examples/textstats.';
const MODES = {
  checking: { badge: 'Checking backend…', notice: 'Checking for the local backend…' },
  replay: {
    badge: 'Connected · replay only',
    notice: 'Connected · replay only. Replaying recorded runs from this machine’s ledgers. A replay is a recording, not a new run. Nothing is launched and links are never fetched.',
  },
  launch: {
    badge: 'Connected · launch enabled',
    notice: 'Connected · launch enabled. An analyst agent reads the public GitHub repo you paste, which uses Crusoe credits. Nothing runs until you approve a proposal, and repo code only ever runs in the Daytona sandbox.',
  },
  down: { badge: 'Demo mode · backend disconnected', notice: 'Demo mode · local scenes and scripted chat. Backend disconnected. Links are never fetched.' },
};
const ANALYST_STATUS = ['proposal', 'questions', 'needs_input', 'not_feasible'];
const HINT_TAIL = 'Enter to send · Shift+Enter for a new line';
const APPROVE_TEXT = 'Runs prepare in the Daytona sandbox (~2 min) and a live agent run (≤30 min, Crusoe ≤ config cap). Continue?';
const PREPARE_LABEL = 'Preparing sandbox: generating hidden cases and recording the original function’s outputs';
const MODE_COPY = {
  start: { hint: `Send to analyze the repo · you confirm before anything is spent · ${HINT_TAIL}`, placeholder: 'What should be optimized? e.g. make the CSV parser faster', send: 'Analyze ↗', label: 'Analyze the repo with the analyst agent' },
  answer: { hint: `Your next message goes to the analyst agent (uses Crusoe credits) · ${HINT_TAIL}`, placeholder: 'Answer the analyst…', send: '↑', label: 'Send answer to the analyst' },
  idle: { hint: 'Use Retry or Try a different prompt on the message above', placeholder: 'Use the buttons on the message above…', send: '↑', label: 'Send message' },
  decide: { hint: 'Choose Approve & run or Suggest changes on the proposal; nothing is sent until you do', placeholder: 'Approve the proposal, or choose Suggest changes first…', send: '↑', label: 'Send message' },
  busy: { hint: 'The analyst is working · you can answer when it responds', placeholder: 'The analyst is working…', send: '↑', label: 'Send message' },
  liveRun: { hint: `Answers come from this run’s ledger data · ${HINT_TAIL}`, placeholder: 'Ask about this run, or tell me what’s next…', send: '↑', label: 'Send message' },
  run: { hint: `Answers come from the recorded run’s data · ${HINT_TAIL}`, placeholder: 'Ask about this run, or tell me what’s next…', send: '↑', label: 'Send message' },
  demo: { hint: `Local scripted replies · ${HINT_TAIL}`, placeholder: 'Ask about this run, or tell me what’s next…', send: '↑', label: 'Send message' },
};

const app = document.querySelector('[data-app="sidecar"]');
const input = app.querySelector('textarea');
const source = app.querySelector('input');
const error = app.querySelector('.source-error');
const note = app.querySelector('.source-note');
const conversation = app.querySelector('.conversation-scroll');
const messages = app.querySelector('.chat-extra');
const sendButton = app.querySelector('.send');
const newRunButton = app.querySelector('.new-run');
const modeChip = app.querySelector('.mode-chip');
const pasteLink = app.querySelector('.paste-link');
const hint = app.querySelector('.chat-hint');
const view = {
  element: app,
  phase: 0,
  sourceOpen: true,
  playing: false,
  timer: null,
  api: 'checking',
  launch: false,
  analysis: null, // { repoUrl, prompt, proposalId, last, awaiting, failed }
  job: null, // the tracked launch job
  busy: false, // a launch request is in flight
  runs: [],
  runsError: '',
  run: null,
  speed: 8,
  follow: true,
  artifactUrl: adapter.artifactUrl,
};
let chatController = null;
let frame = 0;
let ticker = null;
const clock = (ms) => { const t = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`; };
const jobActive = () => !!view.job && isJobActive(view.job.status);
const modeKey = () => (view.api === 'up' ? (view.launch ? 'launch' : 'replay') : view.api);

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
  syncUi();
}

function redraw() {
  render(view, false);
  syncUi();
}

function refresh() {
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    render(view, false);
    syncUi();
  });
}

function composerMode() {
  if (view.run) return view.run.live ? 'liveRun' : 'run';
  if (view.analysis) return jobActive() ? 'busy' : view.analysis.awaiting ? 'answer' : view.analysis.last?.type === 'result' ? 'decide' : 'idle';
  if (view.launch && view.api === 'up' && view.phase === 0) return 'start';
  return 'demo';
}

function applyMode() {
  const mode = MODES[modeKey()];
  app.querySelector('.demo-badge').textContent = mode.badge;
  app.querySelector('.demo-notice').textContent = mode.notice;
  const key = composerMode();
  const copy = MODE_COPY[key];
  hint.textContent = key === 'demo' && view.api === 'up' && view.phase === 0 ? MODE_COPY.run.hint : copy.hint;
  input.placeholder = key === 'demo' && view.phase === 0
    ? (view.api === 'up' ? 'Ask about a run once you’ve chosen one…' : 'Tell me what you want changed…')
    : copy.placeholder;
  sendButton.textContent = key === 'demo' && view.phase === 0 ? (view.api === 'up' ? 'Use link ↗' : 'Preview flow ↗') : copy.send;
  sendButton.setAttribute('aria-label', key === 'demo' && view.phase === 0
    ? (view.api === 'up' ? 'Use code link (not enabled yet)' : 'Start local preview')
    : copy.label);
  const launch = view.launch && view.api === 'up';
  source.type = launch ? 'text' : 'url';
  source.placeholder = launch ? 'https://github.com/owner/repo' : 'https://github.com/your-team/repo/blob/main/parser.py';
  source.setAttribute('aria-label', launch ? 'GitHub repo link' : 'Code link');
  app.querySelector('.source-label').textContent = launch ? 'GitHub repo' : 'Code link';
  note.textContent = launch ? SOURCE_NOTE : LAUNCH_NOTE;
  note.hidden = view.api !== 'up';
  const answering = key === 'answer';
  modeChip.hidden = !answering;
  modeChip.textContent = answering ? 'Answering the analyst · uses Crusoe credits' : '';
  pasteLink.hidden = !!view.analysis && !view.run;
  pasteLink.textContent = launch ? '↗ Paste a GitHub link' : '↗ Paste a code link';
}

// Re-derives every control that depends on launch state: disabled flags stop a double spend while a request or job is live.
function syncUi() {
  applyMode();
  const lock = view.busy || jobActive();
  messages.querySelectorAll('.card button[data-act]').forEach((button) => {
    button.disabled = lock || button.dataset.unavailable === '1' || button.closest('.card').classList.contains('stale');
  });
  sendButton.disabled = view.busy;
  newRunButton.disabled = jobActive();
  newRunButton.title = jobActive() ? 'Cancel the running job first' : '';
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
      redraw();
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
    addMessage('agent', message, `✳ No Free Wins · ${run.live ? 'live' : 'replay'}`, 'replay-event');
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
  if (run.live) {
    try { run.presentation = await adapter.presentation(run.id, run.controller.signal); } catch { /* optional source preview */ }
  }
  if (view.run !== run) return;
  if (view.follow && view.phase !== 6) go(6, true);
  else go(view.phase, false);
}

// Replays a recorded run, or (live) follows the ledger of a run just launched from this page.
async function replay(meta, { live = false } = {}) {
  abortReplay();
  stop();
  chatController?.abort();
  const run = newRun(meta);
  run.presentationPromise = adapter.presentation(meta.runId, run.controller.signal).then(
    data => { run.presentation = data; if (view.run === run) refresh(); },
    () => {},
  );
  run.live = live;
  view.run = run;
  view.follow = true;
  view.sourceOpen = false;
  error.hidden = true;
  clearReplayMessages();
  if (!live) {
    app.querySelector('.history .attachment').textContent = `↗ Recorded run · ${meta.label || meta.runId}`;
    app.querySelector('.history .user .chat-body').textContent = `Replay the recorded run ${meta.runId}.`;
  }
  go(1, true);
  try {
    const options = { speed: SPEED_PARAM[view.speed], onEnd: (state) => { run.endState = state; } };
    for await (const event of adapter.events(meta.runId, run.controller.signal, options)) {
      if (view.run !== run) return;
      if (event.kind === 'candidate_created') await showAttempts(run, event);
      if (run.controller.signal.aborted || view.run !== run) return;
      onEvent(run, event);
      if (event.kind === 'acceptance_result' && !run.live) {
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
    if (run.live) loadRuns().then(() => { if (!view.run && view.phase === 0) redraw(); });
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
  if (run.live || attempts.length < 2 || view.speed === 'instant') return;
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
    view.launch = health.launch === true;
    await loadRuns();
  } catch {
    view.api = 'down';
    view.launch = false;
  }
  applyMode();
  go(view.phase, false);
}

// ---- analyst flow (launch enabled) -------------------------------------------------------
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function actionButton(label, act, className = 'nfw-secondary') {
  const button = el('button', `card-action ${className}`, label);
  button.type = 'button';
  button.dataset.act = act;
  return button;
}

function newCard(speakerText, extra = '') {
  const card = el('article', `message agent card${extra ? ` ${extra}` : ''}`);
  card.append(el('div', 'speaker', speakerText));
  return card;
}

function pushCard(card) {
  messages.append(card);
  syncUi();
  // Start a result card at its top so long proposals are read from the heading down.
  const delta = card.getBoundingClientRect().top - conversation.getBoundingClientRect().top;
  conversation.scrollTop = Math.max(0, conversation.scrollTop + delta - 8);
}

const markStale = () => messages.querySelectorAll('.card').forEach((card) => card.classList.add('stale'));
const textList = (items, ordered = false) => {
  const list = el(ordered ? 'ol' : 'ul', 'plain-list');
  items.forEach((item) => list.append(el('li', '', String(item))));
  return list;
};
const strings = (value) => (Array.isArray(value) ? value.filter((item) => typeof item === 'string' && item.trim()) : []);
const trimError = (value) => (typeof value === 'string' && value.trim() ? value.trim().slice(0, 600) : 'The job failed without a message.');

function requestError(failure) {
  if (failure instanceof ApiError) return failure.message;
  if (failure instanceof TypeError) return 'Could not reach the local bridge. Nothing was started.';
  return failure instanceof Error ? failure.message : 'The request failed. Nothing was started.';
}

function codeSection(summary, source) {
  const details = el('details', 'card-details');
  details.append(el('summary', 'cursor-interaction', summary));
  details.append(el('pre', 'code-preview nfw-mono', source));
  return details;
}

function proposalCard(result) {
  const p = result.proposal && typeof result.proposal === 'object' ? result.proposal : {};
  const card = newCard('✳ Analyst · proposed contract');
  card.append(el('h3', 'card-title', 'Proposed optimization contract'));
  card.append(el('p', 'card-target nfw-mono', typeof p.target === 'string' && p.target ? p.target : 'target not given'));
  if (typeof p.description === 'string' && p.description) card.append(el('p', 'chat-body', p.description));
  const hints = strings(p.hints);
  if (hints.length) { card.append(el('h4', 'mini-heading', 'Planned optimizations')); card.append(textList(hints)); }
  const risks = strings(p.risks);
  if (risks.length) { card.append(el('h4', 'mini-heading', 'Risks')); card.append(textList(risks)); }
  const sizes = p.sizes && typeof p.sizes === 'object' ? p.sizes : {};
  const correctness = Array.isArray(sizes.correctness_sizes) ? sizes.correctness_sizes.filter((n) => Number.isFinite(n)) : [];
  const sizeText = [Number.isFinite(sizes.timing_size) ? `timing size ${sizes.timing_size}` : '', correctness.length ? `correctness sizes ${correctness.join(', ')}` : ''].filter(Boolean).join(' · ');
  if (sizeText) card.append(el('p', 'nfw-muted', `Sizes · ${sizeText}`));
  const included = strings(p.include);
  if (included.length) card.append(el('p', 'nfw-muted', `Included files · ${included.join(', ')}`));
  if (typeof p.generator_source === 'string' && p.generator_source) card.append(codeSection('Input generator source (runs only in the sandbox)', p.generator_source));
  if (typeof result.target_source_excerpt === 'string' && result.target_source_excerpt) card.append(codeSection('Target source excerpt', result.target_source_excerpt));
  const actions = el('div', 'card-actions');
  const approve = actionButton('Approve & run', 'approve', 'nfw-primary');
  if (!result.proposal || !view.analysis?.proposalId) approve.dataset.unavailable = '1';
  actions.append(approve, actionButton('Suggest changes', 'suggest'));
  card.append(actions);
  return card;
}

function questionsCard(result) {
  const card = newCard('✳ Analyst · questions');
  card.append(el('h3', 'card-title', 'The analyst has questions'));
  if (typeof result.reason === 'string' && result.reason) card.append(el('p', 'chat-body', result.reason));
  const questions = strings(result.questions);
  if (questions.length) card.append(textList(questions, true));
  card.append(el('p', 'nfw-muted', 'Type your answer below and send it.'));
  return card;
}

function infeasibleCard(result) {
  const card = newCard('✳ Analyst · not feasible');
  card.append(el('h3', 'card-title', 'Not feasible as asked'));
  card.append(el('p', 'chat-body', typeof result.reason === 'string' && result.reason ? result.reason : 'The analyst did not give a reason.'));
  const suggestions = strings(result.suggestions);
  if (suggestions.length) { card.append(el('h4', 'mini-heading', 'Possible optimizations / alternatives')); card.append(textList(suggestions)); }
  const actions = el('div', 'card-actions');
  actions.append(actionButton('Try a different prompt', 'reprompt'));
  card.append(actions);
  return card;
}

function failedCard(kind, message, beforeRun, cancelled = false) {
  const card = newCard(`✳ ${kind === 'approve' ? 'Prepare' : 'Analysis'} ${cancelled ? 'cancelled' : 'failed'}`, 'card-failed');
  card.append(el('p', 'chat-body', message));
  const actions = el('div', 'card-actions');
  if (kind === 'approve') {
    if (beforeRun) actions.append(actionButton('Ask the analyst to fix it', 'fix', 'nfw-primary'));
  } else {
    actions.append(actionButton('Retry', 'retry', 'nfw-primary'));
    if (kind === 'analyze') actions.append(actionButton('Try a different prompt', 'reprompt'));
  }
  if (actions.children.length) card.append(actions);
  return card;
}

function showResult(result) {
  const a = view.analysis;
  const status = result.status;
  a.awaiting = status === 'questions' || status === 'needs_input';
  markStale();
  if (status === 'proposal') pushCard(proposalCard(result));
  else if (status === 'not_feasible') pushCard(infeasibleCard(result));
  else pushCard(questionsCard(result));
  if (a.awaiting) input.focus();
}

function settleConfirm(card, text) {
  card.classList.add('settled');
  card.confirm = null;
  card.querySelector('.card-actions')?.replaceWith(el('p', 'nfw-muted', text));
}

function confirmCard(text, confirmLabel, detail, onConfirm) {
  messages.querySelectorAll('.confirm-card:not(.settled)').forEach((old) => settleConfirm(old, 'Superseded.'));
  const card = el('article', 'message agent confirm-card');
  card.append(el('div', 'speaker', '✳ Confirm'));
  card.append(el('p', 'chat-body', text));
  if (detail) card.append(el('p', 'nfw-muted card-quote', detail));
  const actions = el('div', 'card-actions');
  actions.append(actionButton(confirmLabel, 'confirm', 'nfw-primary'), actionButton('Cancel', 'confirm-cancel'));
  card.append(actions);
  card.confirm = onConfirm;
  messages.append(card);
  conversation.scrollTop = conversation.scrollHeight;
  actions.querySelector('button').focus();
}

async function onConfirmClick(button) {
  const card = button.closest('.confirm-card');
  if (!card || card.classList.contains('settled') || !card.confirm) return;
  if (view.busy || jobActive()) return;
  const run = card.confirm;
  card.querySelectorAll('button').forEach((b) => { b.disabled = true; }); // synchronous: a second click cannot spend again
  const ok = await run();
  settleConfirm(card, ok ? 'Confirmed.' : 'Not started.');
}

function progressLabel(job) {
  if (job.kind === 'approve') return job.runStarted ? 'Run started; following its live ledger' : PREPARE_LABEL;
  return job.kind === 'answer' ? 'Analyst is revising the proposal' : 'Reading the repo';
}

function addProgress(job) {
  const article = el('article', 'message agent progress');
  article.append(el('div', 'speaker', job.kind === 'approve' ? '✳ Sandbox' : '✳ Analyst'));
  const body = el('p', 'chat-body');
  job.textNode = el('span', '', '');
  job.clockNode = el('span', 'nfw-muted', '');
  job.clockNode.setAttribute('aria-hidden', 'true'); // ticks every second; keep it out of the live region
  body.append(job.textNode, document.createTextNode(' '), job.clockNode);
  article.append(body);
  messages.append(article);
  updateProgress(job);
  conversation.scrollTop = conversation.scrollHeight;
}

function updateProgress(job) {
  if (!job.textNode || !jobActive()) return;
  const phase = job.phase && !job.runStarted ? ` · ${String(job.phase).slice(0, 80)}` : '';
  job.textNode.textContent = `${progressLabel(job)}…${phase}`;
  job.clockNode.textContent = `(elapsed ${clock(Date.now() - job.startedAt)})`;
}

function startTicker() {
  clearInterval(ticker);
  ticker = setInterval(() => {
    if (!jobActive()) { clearInterval(ticker); ticker = null; return; }
    updateProgress(view.job);
    const text = clock(Date.now() - view.job.startedAt);
    app.querySelectorAll('[data-elapsed]').forEach((node) => { node.textContent = text; });
  }, 1000);
}

function stopTicker() {
  clearInterval(ticker);
  ticker = null;
}

function finishProgress(job, text) {
  if (!job.textNode) return;
  job.textNode.textContent = text;
  job.clockNode.textContent = `(${clock(Date.now() - job.startedAt)})`;
}

// Sends one launch request. Only called after an explicit confirmation or a user-sent answer.
async function launchJob(kind, send, before) {
  if (view.busy || jobActive()) return false;
  view.busy = true;
  syncUi();
  const controller = new AbortController();
  try {
    const started = await send(controller.signal);
    before?.();
    trackJob(kind, started, controller);
    return true;
  } catch (failure) {
    addMessage('agent', requestError(failure), '✳ Launch request failed', 'notice-error');
    return false;
  } finally {
    view.busy = false;
    syncUi();
  }
}

function trackJob(kind, started, controller) {
  const job = {
    kind,
    id: started.jobId,
    status: 'queued',
    phase: '',
    startedAt: Date.now(),
    controller,
    sessionId: typeof started.sessionId === 'string' ? started.sessionId : null,
    cancelling: false,
    runStarted: false,
    textNode: null,
    clockNode: null,
  };
  view.job = job;
  addProgress(job);
  startTicker();
  go(1, kind === 'analyze');
  adapter.pollJob(job.id, controller.signal, (snapshot) => onJobUpdate(job, snapshot)).then(
    (snapshot) => { if (snapshot) onJobEnd(job, snapshot); },
    (failure) => { if (!controller.signal.aborted) onJobLost(job, failure); },
  );
}

function onJobUpdate(job, snapshot) {
  if (view.job !== job) return;
  job.status = snapshot.status;
  job.phase = typeof snapshot.phase === 'string' ? snapshot.phase : '';
  if (job.kind === 'approve' && !job.runStarted && typeof snapshot.runId === 'string' && snapshot.runId && isJobActive(snapshot.status)) startLive(job, snapshot);
  updateProgress(job);
  if (!view.run) refresh();
  else syncUi();
}

function startLive(job, snapshot) {
  job.runStarted = true;
  markStale();
  const meta = {
    runId: snapshot.runId,
    sessionId: typeof snapshot.sessionId === 'string' ? snapshot.sessionId : job.sessionId || '',
    kind: 'repo',
    label: `Live run · ${repoShort(view.analysis?.repoUrl)}`,
    state: 'running',
    startedUtc: snapshot.startedUtc || null,
    endedUtc: null,
    taskId: '',
    verified: null,
  };
  addMessage('agent', `Sandbox ready. Run ${snapshot.runId} started; following its ledger live. Speedups are only claimed from the audited result at the end.`, '✳ Live run');
  replay(meta, { live: true });
}

function onJobLost(job, failure) {
  if (view.job !== job) return;
  job.status = 'failed';
  stopTicker();
  const message = `Lost contact with the job (${requestError(failure)}). It may still be running on the bridge; reload to check recorded runs.`;
  finishProgress(job, 'Job status unavailable');
  addMessage('agent', message, '✳ Launch', 'notice-error');
  if (view.analysis) view.analysis.last = { type: 'failed', kind: job.kind, error: message };
  redraw();
}

function onJobEnd(job, snapshot) {
  if (view.job !== job) return;
  stopTicker();
  job.status = snapshot.status;
  const a = view.analysis;
  if (job.kind === 'approve') {
    loadRuns().then(() => { if (!view.run && view.phase === 0) redraw(); });
    if (snapshot.status === 'done' && job.runStarted) {
      finishProgress(job, 'Run finished');
    } else if (snapshot.status === 'cancelled') {
      finishProgress(job, 'Cancelled');
      addMessage('agent', job.runStarted ? 'The run was cancelled. No accepted result is claimed.' : 'Prepare was cancelled. Nothing was run.', '✳ Sandbox');
      if (!job.runStarted && a) a.last = { type: 'cancelled', kind: 'approve' };
    } else {
      const message = snapshot.status === 'done' ? 'Prepare finished without starting a run.' : trimError(snapshot.error);
      finishProgress(job, job.runStarted ? 'Run job failed' : 'Prepare failed');
      if (a && !job.runStarted) a.last = { type: 'failed', kind: 'approve', error: message };
      pushCard(failedCard('approve', job.runStarted ? `The run job failed: ${message}` : message, !job.runStarted));
    }
    redraw();
    return;
  }
  if (snapshot.status === 'cancelled') {
    finishProgress(job, 'Cancelled');
    a.last = { type: 'cancelled', kind: job.kind };
    a.awaiting = job.kind === 'answer';
    if (job.kind === 'answer') addMessage('agent', 'Cancelled. You can send your answer again.', '✳ Analyst');
    else pushCard(failedCard('analyze', 'Analysis cancelled.', true, true));
  } else if (snapshot.status === 'failed') {
    const message = trimError(snapshot.error);
    finishProgress(job, 'Failed');
    a.last = { type: 'failed', kind: job.kind, error: message };
    a.awaiting = false;
    pushCard(failedCard(job.kind, message, true));
  } else {
    const result = snapshot.result;
    if (!result || typeof result !== 'object' || !ANALYST_STATUS.includes(result.status)) {
      const message = 'The analyst returned a result this page could not read.';
      finishProgress(job, 'Failed');
      a.last = { type: 'failed', kind: job.kind, error: message };
      a.awaiting = false;
      pushCard(failedCard(job.kind, message, true));
    } else {
      finishProgress(job, 'Analyst finished');
      if (typeof result.proposal_id === 'string' && result.proposal_id) a.proposalId = result.proposal_id;
      a.last = { type: 'result', result };
      showResult(result);
    }
  }
  redraw();
}

function resetAnalysis() {
  markStale();
  view.analysis = null;
  view.job = null;
  view.sourceOpen = true;
  input.value = '';
}

function returnToAnalysis() {
  if (view.run && !view.run.live) {
    abortReplay();
    clearReplayMessages();
    view.run = null;
  }
  go(1);
}

function requestAnalyze() {
  const repoUrl = source.value.trim();
  const prompt = input.value.trim();
  const problem = repoUrlProblem(repoUrl);
  if (problem) {
    error.textContent = problem;
    error.hidden = false;
    source.setAttribute('aria-invalid', 'true');
    source.focus();
    return;
  }
  source.removeAttribute('aria-invalid');
  if (!prompt) {
    error.textContent = 'Say what should be faster, for example “make the CSV parser faster”.';
    error.hidden = false;
    input.focus();
    return;
  }
  if (jobActive() || view.busy) {
    error.textContent = 'Another job is running';
    error.hidden = false;
    return;
  }
  error.hidden = true;
  confirmCard(
    `Analyze ${repoShort(repoUrl)} with the analyst agent? Uses Crusoe credits (capped by the config, about $1 max). Nothing runs on your machine; repo code only ever runs in the Daytona sandbox later, after you approve.`,
    'Analyze',
    `Prompt: ${prompt}`,
    () => startAnalysis(repoUrl, prompt),
  );
}

function startAnalysis(repoUrl, prompt) {
  return launchJob('analyze', (signal) => adapter.analyze(repoUrl, prompt, signal), () => {
    stop();
    abortReplay();
    clearReplayMessages();
    markStale();
    view.analysis = { repoUrl, prompt, proposalId: null, last: null, awaiting: false, lastAnswer: '' };
    view.run = null;
    view.follow = true;
    view.sourceOpen = false;
    app.querySelector('.history .attachment').textContent = `↗ ${repoShort(repoUrl)}`;
    app.querySelector('.history .user .chat-body').textContent = prompt;
    input.value = '';
  });
}

function retryAnalysis(a) {
  return launchJob('analyze', (signal) => adapter.analyze(a.repoUrl, a.prompt, signal), () => {
    a.last = null;
    markStale();
  });
}

function sendAnswer(a, text) {
  a.lastAnswer = text;
  return launchJob('answer', (signal) => adapter.answer(a.proposalId, text, signal), () => markStale());
}

function analystChat(text) {
  const a = view.analysis;
  if (view.busy || jobActive()) {
    addMessage('agent', 'The analyst is still working. Send your message once it responds.', '✳ Analyst');
    return;
  }
  if (!a.awaiting) {
    addMessage('agent', 'The proposal is waiting for your decision. Choose Approve & run, or Suggest changes to send feedback to the analyst.', '✳ Analyst');
    return;
  }
  if (!a.proposalId) {
    addMessage('agent', 'There is no proposal to answer yet. Try a different prompt.', '✳ Analyst');
    return;
  }
  addMessage('user', text);
  input.value = '';
  a.awaiting = false;
  sendAnswer(a, text).then((ok) => {
    if (ok) return;
    a.awaiting = true;
    input.value = text;
    syncUi();
  });
}

async function cancelJob() {
  const job = view.job;
  if (!job || !jobActive() || job.cancelling) return;
  job.cancelling = true;
  refresh();
  try {
    await adapter.cancel(job.id);
  } catch (failure) {
    job.cancelling = false;
    addMessage('agent', `Could not cancel: ${requestError(failure)}`, '✳ Launch', 'notice-error');
    refresh();
  }
}

function onAction(act, button) {
  const a = view.analysis;
  if (act === 'confirm') { onConfirmClick(button); return; }
  if (act === 'confirm-cancel') {
    const card = button.closest('.confirm-card');
    if (card) settleConfirm(card, 'Cancelled. Nothing was sent.');
    return;
  }
  if (act === 'cancel-job') { cancelJob(); return; }
  if (!a || view.busy || jobActive()) return;
  if (act === 'approve') {
    if (!a.proposalId) return;
    returnToAnalysis();
    confirmCard(APPROVE_TEXT, 'Approve & run', '', () => launchJob('approve', (signal) => adapter.approve(a.proposalId, signal)));
  } else if (act === 'suggest') {
    returnToAnalysis();
    a.awaiting = true;
    addMessage('agent', 'Tell me what to change. Your next message goes to the analyst agent, which will revise the proposal.', '✳ Analyst');
    syncUi();
    input.focus();
  } else if (act === 'reprompt') {
    resetAnalysis();
    addMessage('agent', 'Ready for a different prompt. The repo link is still filled in.', '✳ Analyst');
    go(0);
    input.focus();
  } else if (act === 'retry') {
    const failed = a.last;
    if (!failed || !['failed', 'cancelled'].includes(failed.type)) return;
    returnToAnalysis();
    if (failed.kind === 'analyze') {
      confirmCard(`Run the analysis of ${repoShort(a.repoUrl)} again? Uses Crusoe credits.`, 'Analyze again', `Prompt: ${a.prompt}`, () => retryAnalysis(a));
    } else if (a.lastAnswer && a.proposalId) {
      const text = a.lastAnswer;
      confirmCard('Send your answer to the analyst again? Uses Crusoe credits.', 'Send again', `Answer: ${text}`, () => sendAnswer(a, text));
    }
  } else if (act === 'fix') {
    const failed = a.last;
    if (!failed || failed.type !== 'failed' || failed.kind !== 'approve' || !a.proposalId) return;
    returnToAnalysis();
    const text = `Prepare failed in the Daytona sandbox with this error: ${failed.error}\nPlease revise the proposal (target, input generator or sizes) so prepare succeeds, or say if it is not feasible.`.slice(0, 1800);
    confirmCard('Send the prepare error to the analyst so it can fix the proposal? Uses Crusoe credits.', 'Ask the analyst', `Error: ${failed.error}`, () => sendAnswer(a, text));
  }
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
  if (!view.run && view.analysis) {
    analystChat(text.trim());
    return;
  }
  addMessage('user', text);
  if (view.run) {
    input.value = '';
    askRun(view.run, text.trim());
    return;
  }
  let answer;
  if (/\bstop\b|\bpause\b/i.test(text)) {
    stop();
    redraw();
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
  if (jobActive()) return;
  abortReplay();
  view.analysis = null;
  view.job = null;
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
  if (view.run || view.phase !== 0 || view.analysis) {
    chat(input.value);
    return;
  }
  if (view.api === 'up' && view.launch) {
    requestAnalyze();
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
      redraw();
    } else startExample();
  } else if (button.hasAttribute('data-act')) {
    onAction(button.dataset.act, button);
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
    redraw();
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
    if (jobActive()) return;
    stop();
    abortReplay();
    chatController?.abort();
    view.run = null;
    view.analysis = null;
    view.job = null;
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
  stopTicker();
  view.job?.controller.abort();
});
applyMode();
redraw();
connect();
