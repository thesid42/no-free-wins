import { phaseNames, phaseNotes, proof, recordedCode } from './demo-data.js';
import { questions, snippet, stageCopy, strategyLabel, workerState } from './presentation.js';

const esc = s => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const fx = n => Number.isFinite(n) ? `${n.toFixed(2)}×` : '—';
const who = k => `Worker ${String(k).replace(/^worker_?/i, '').toUpperCase()}`;
const model = id => /nano/i.test(id || '') ? 'Nemotron Nano' : /nemotron/i.test(id || '') ? 'Nemotron Super' : /glm/i.test(id || '') ? 'GLM verifier' : String(id || 'Model pending').split('/').pop();
const chip = (s, kind = '') => `<span class="chip ${kind ? `chip-${kind}` : ''}">${esc(s)}</span>`;
const heading = n => `<div class="scene-heading"><h2>${stageCopy[n][0]}</h2><p class="stage-sub">${stageCopy[n][1]}</p></div>`;
const link = (v, id, label, cls = 'nfw-secondary') => `<a class="nfw-link ${cls}" href="${esc(v.artifactUrl(id))}" download>${label}</a>`;

function controls(v) {
  const r = v.run;
  return `<div class="stage-titlebar"><span class="eyebrow">${r ? 'Recorded replay' : 'Example preview'} <span class="chapter-number">${String(Math.max(1, v.phase)).padStart(2, '0')} / 06</span></span><div class="scene-controls">${r ? `<div class="speed-toggle" role="group" aria-label="Replay speed">${[['1', '1×'], ['8', '8×'], ['instant', 'Instant']].map(([s, t]) => `<button type="button" data-speed="${s}" aria-pressed="${String(v.speed) === s}">${t}</button>`).join('')}</div><button type="button" data-replay>${r.status === 'streaming' ? 'Pause replay' : 'Replay run'}</button>${!v.follow && r.status === 'streaming' ? '<button type="button" data-follow>Follow replay ↗</button>' : ''}` : `<button type="button" data-play>${v.playing ? 'Pause preview' : 'Play preview'}</button>`}</div></div>`;
}

function start(v) {
  const items = v.runs.map(r => `<li><button type="button" class="run-item" data-run="${esc(r.runId)}"><span class="run-main"><span class="record-icon">↗</span><strong>${r.kind === 'repo' ? 'Repository optimization' : 'Log parser optimization'}</strong>${chip(r.verified === true ? 'Verified' : /failed|exhausted/.test(r.state) ? 'Stopped' : 'Recorded', r.verified === true ? 'ok' : '')}</span><span class="run-meta"><span>${esc(r.sessionId || r.runId)}</span><span>${new Date(r.startedUtc).toLocaleDateString([], { month: 'short', day: 'numeric' })}</span></span></button></li>`).join('');
  return `<div class="input-scene"><div class="start-orbit" aria-hidden="true"><span>nfw↗</span><i></i><i></i></div><h2>A fair race.<br>A provable win.</h2><p class="stage-sub">Watch two strategies become one checked result.<br>Your conversation stays open.</p>${v.api === 'up' ? `<div class="run-picker"><div class="picker-label">Your recorded runs <span>${v.runs.length}</span></div>${items ? `<ul class="run-list" aria-label="Recorded runs">${items}</ul>` : '<p class="run-empty">No recordings on this machine yet.</p>'}</div>` : ''}<button type="button" class="nfw-secondary" data-example>Explore the example ↗</button><div class="supported">${v.api === 'up' ? 'Recorded evidence · launching from a link is coming next' : 'Local demonstration · no models or sandboxes running'}</div></div>`;
}

function analyze(v) {
  const d = v.run?.data, strategies = d?.strategyIds || ['One pass aggregation', 'Reduce repeated parsing'];
  return heading(1) + `<div class="contract-card"><span class="contract-icon">⌘</span><div><span class="section-label">The contract</span><h3>Same input. Exactly the same output.</h3><p class="nfw-muted">The original implementation defines the reference.</p></div><span class="contract-lock">⌑</span></div><div class="scan-visual" aria-hidden="true"><span></span><span></span><span></span><span></span></div><div class="strategy-plan">${strategies.map((s, i) => `<div><span class="strategy-letter">${String.fromCharCode(65 + i)}</span><span>${esc(strategyLabel(s))}</span><span class="plan-arrow">↗</span></div>`).join('')}</div><div class="scene-footer"><span class="status-dot"></span>${d?.strategyIds.length ? 'Two recorded strategies · one shared contract' : 'Example analysis · contract before candidates'}</div>`;
}

function workers(v) {
  const r = v.run, d = r?.data, keys = d ? Object.keys(d.workers) : ['worker_a', 'worker_b'];
  const selected = d && Object.keys(d.verdicts).find(id => d.verdicts[id].approved === true);
  const recovery = keys.find(k => d?.workers[k].visualAttempt || d?.workers[k].attempts?.some(a => a.kind === 'fallback' && a.outcome === 'ok'));
  const rw = recovery && d.workers[recovery];
  const banner = recovery ? `<div class="recovery-banner" role="status"><span class="recovery-icon">↻</span><div><strong>${esc(rw.visualAttempt ? workerState(r, recovery).label : 'Fallback recovered the task')}</strong><span>${esc(who(recovery))} · ${d.faults.length ? 'labeled fault drill' : 'recorded recovery'} · recorded attempt history</span></div></div>` : '';
  const cards = keys.map((key, i) => {
    const w = d?.workers[key], s = r ? workerState(r, key) : { kind: i ? 'pending' : 'working', label: i ? 'Queued' : 'Exploring this strategy' };
    const chosen = selected && w?.candidateId === selected;
    const code = w?.candidateId ? snippet(r.presentation.candidates[w.candidateId]) : '';
    const attempts = w?.attempts || [], fallback = w?.visualAttempt?.kind === 'fallback' ? w.visualAttempt : attempts.find(a => a.kind === 'fallback' && a.outcome === 'ok');
    return `<article class="worker worker-${s.kind}${chosen ? ' worker-selected' : ''}" data-worker="${key}" data-worker-state="${s.kind}"><div class="worker-top"><span class="worker-avatar">${i ? 'B' : 'A'}</span><div><h3>${who(key)}</h3><small>${esc(strategyLabel(w?.strategyId || (i ? 'Reduce repeated parsing' : 'One pass aggregation')))}</small></div><span class="worker-indicator"></span></div><div class="code-window"><div class="code-window-bar"><span></span><span></span><span></span><small>${w?.candidateId ? 'code excerpt' : 'strategy'}</small></div><pre class="worker-code">${esc(code || (r ? w?.candidateId ? 'Code preview unavailable.\nUse the artifact download.' : 'Waiting for the recorded\ncandidate to arrive…' : i ? 'validate each token\nreduce repeated work' : 'for line in lines:\n    counts.update(tokens(line))'))}</pre></div><div class="worker-status"><span class="status-dot"></span>${chosen ? 'Selected for verification' : esc(s.label)}</div><div class="worker-model">${esc(model(fallback?.model_id || w?.primary || 'Nemotron Super'))}${fallback ? ' · fallback' : ''}</div>${attempts.length > 1 ? `<div class="attempt-trail">${attempts.map(a => `<span class="attempt-${a.outcome === 'ok' ? 'ok' : 'failed'}" title="${esc(a.model_id)}">${a.kind === 'fallback' ? '↻' : a.outcome === 'ok' ? '✓' : '×'} ${esc(a.kind)}</span>`).join('<span class="attempt-arrow">→</span>')}</div>` : ''}</article>`;
  }).join('');
  return heading(2) + banner + `<div class="flow-graph"><div class="flow-node flow-origin"><span>⌘</span>Contract &amp; plan</div><div class="flow-fork" aria-hidden="true"><i class="flow-packet"></i></div><div class="workers">${cards || '<p class="run-empty">Waiting for worker assignments…</p>'}</div><div class="flow-join" aria-hidden="true"><i class="flow-packet"></i></div><div class="flow-node flow-destination${selected ? ' flow-approved' : ''}"><span>V</span><div>Independent verifier<small>${selected ? 'Candidate reviewed ✓' : 'The next handoff'}</small></div></div></div><div class="scene-footer">${r ? 'Recorded worker activity · sequential execution' : 'Illustrative scene · no jobs running'}</div>`;
}

function verify(v) {
  const d = v.run?.data, ids = d ? Object.keys(d.candidates) : ['candidate A'];
  const verdict = d && Object.entries(d.verdicts).at(-1), approved = verdict?.[1].approved === true;
  return heading(3) + `<div class="review-stage"><div class="review-orbit ${approved ? 'review-approved' : ''}"><span>V</span><i></i></div><div class="review-title">${approved ? 'Independent review passed' : verdict ? 'Candidate needs another look' : v.run?.status === 'done' ? 'No verdict recorded' : 'An independent set of eyes'}</div><p class="nfw-muted">${approved ? `${esc(verdict[0])} · approval bound to its source hash` : 'A fast candidate still has to preserve the contract.'}</p></div><div class="candidate-checks">${ids.map(id => { const s = d?.screens[id], x = d?.verdicts[id]; return `<div class="candidate-check"><span class="candidate-mark">${s?.correctness_passed ? '✓' : s?.correctness_passed === false ? '×' : '·'}</span><div><strong>${esc(id)}</strong><small>${s ? s.correctness_passed ? 'Correctness passed' : 'Correctness failed' : v.run ? 'No screen recorded yet' : 'Example candidate review'}</small></div>${x ? chip(x.approved ? 'Approved' : 'Rejected', x.approved ? 'ok' : 'bad') : chip('Not selected')}</div>`; }).join('')}</div><div class="scene-footer">Screens rank candidates. Full acceptance measures the win.</div>`;
}

function race(v) {
  const r = v.run, result = r?.result, has = r?.data.acceptances.length > 0;
  const ok = has && result?.accepted && result.receiptAuditValid;
  const count = ok ? r.race.current || result.timingPairs : 0, total = ok ? r.race.total || result.timingPairs : 30;
  const pair = ok ? r.race.pair || r.presentation.pairTimings.at(-1) : null;
  const ms = s => Number.isFinite(s) ? `${(s * 1000).toFixed(1)} ms` : '—';
  return heading(4) + `<div class="race-board${r && !ok ? ' race-awaiting' : ''}"><div class="race-score"><div><span class="section-label">${ok ? 'Recorded measured pair' : r ? 'Acceptance timing' : 'Illustrative timing replay'}</span><div class="pair-count">${ok ? `Pair <strong>${count}</strong><span> / ${total}</span>` : r ? 'Awaiting evidence' : '30 matched pairs'}</div></div><span class="race-seal">${ok ? '✓ Outputs match' : 'Every output checked'}</span></div><div class="race-lane"><div class="race-label"><span><i class="lane-key"></i>Original</span><strong>${pair ? ms(pair.baselineSeconds) : 'Baseline'}</strong></div><div class="race-track"><span class="runner" data-runner="baseline">›</span><span class="finish-line"></span></div></div><div class="race-lane"><div class="race-label"><span><i class="lane-key optimized"></i>Optimized</span><strong>${pair ? ms(pair.candidateSeconds) : 'Candidate'}</strong></div><div class="race-track"><span class="runner fast" data-runner="candidate">»</span><span class="finish-line"></span></div></div><div class="pair-ledger" aria-label="${ok ? `${count} of ${total} recorded pairs shown` : 'Timing progress is not available yet'}">${Array.from({ length: 30 }, (_, i) => `<span class="${ok && i < count ? 'pair-done' : ''}${ok && i === count - 1 ? ' pair-current' : ''}"></span>`).join('')}</div><div class="race-readout"><span>${ok ? `${fx(result.medianSpeedup)} median · 95% CI ${result.confidenceInterval95.map(fx).join('–')}` : r ? has ? 'Recorded event · acceptance not confirmed' : 'No measured progress until evidence arrives' : '3.75× recorded development example'}</span>${ok && r.presentation.pairTimings.length ? `<button type="button" class="text-button" data-pairs ${r.race.playing ? 'disabled' : ''}>${r.race.playing ? 'Replaying pairs…' : 'Replay measured pairs ↻'}</button>` : ''}</div></div><div class="scene-footer">${ok ? 'Measured values from the audited receipt · animated replay of serial trials' : r ? 'The ledger records an aggregate result; this view never invents pair progress.' : 'Illustrative motion · this screen performs no benchmark'}</div>`;
}

function zoo() {
  const animals = [['🦊', 'Clock tamper', 'Trusted timer'], ['🦝', 'Hardcoding', 'Hidden inputs'], ['🐇', 'Early exit', 'Exact outputs'], ['🦥', 'Lazy return', 'Materialized results']];
  return `<div class="separate-suite">↗ Separate defense demo</div>${heading(5)}<div class="zoo">${animals.map(([a, n, c], i) => `<article class="animal" data-animal="${i}"><span class="animal-index">0${i + 1}</span><span class="creature" aria-hidden="true">${a}</span><span class="animal-name">${n}</span><span class="animal-defense">${c}</span><span class="animal-verdict">✓ Rejected</span></article>`).join('')}</div><div class="zoo-summary"><div><strong>4 / 4</strong><span>planted attacks rejected</span></div><div class="honest-controls"><span class="honest-icons" aria-hidden="true">✓ ✓ ✓ ✓</span><strong>4 / 4 honest controls passed</strong></div></div><div class="scene-footer">Recorded development pilot · independent of the optimization run</div><button type="button" class="text-button" data-zoo>Replay the zoo scene ↻</button>`;
}

function changes(run) {
  if (!run.changes.open) return '';
  if (run.changes.state !== 'ready') return `<div class="changes-panel" role="status">${run.changes.state === 'error' ? 'Comparison unavailable. Use the source downloads to inspect the files.' : 'Loading the original and optimized source…'}</div>`;
  const diff = run.changes.diff;
  return `<section class="changes-panel" aria-label="Source changes"><div class="diff-heading"><span>Original → optimized</span><span><b class="added-text">+${diff.rows.filter(r => r.kind === 'added').length}</b> <b class="removed-text">−${diff.rows.filter(r => r.kind === 'removed').length}</b></span></div><p class="diff-note">Original target module → generated solve(data) module. Imports and the entry point can differ.</p><div class="source-diff">${diff.rows.map(row => `<div class="diff-line diff-${row.kind}"><span class="diff-number">${row.kind === 'removed' ? row.oldLine : row.newLine}</span><span class="diff-sign">${row.kind === 'added' ? '+' : row.kind === 'removed' ? '−' : ' '}</span><code>${esc(row.text) || ' '}</code></div>`).join('')}</div>${diff.truncated ? '<p class="nfw-muted">Preview limited to 200 lines per file. Downloads contain the full source.</p>' : ''}</section>`;
}

function proofPanel(v, r) {
  if (!(v.run || v).proofOpen) return '';
  return `<section class="proof-panel" aria-label="Verified proof"><div class="proof-heading">Evidence you can inspect <span>✓ Audited</span></div><div class="proof-data">${[['Candidate', r.candidateId || 'Recorded example'], ['Receipt SHA-256', r.receiptSha256], ['Source SHA-256', r.sourceSha256], ['Ledger final SHA-256', r.ledgerFinalSha256]].filter(([, s]) => s).map(([k, s]) => `<div><span>${k}</span><code>${esc(s)}</code></div>`).join('')}</div>${v.run ? link(v, r.proofArtifactId, '↓ Download proof summary') : '<button type="button" data-download="proof" class="nfw-secondary">↓ Example proof summary</button>'}</section>`;
}

function result(v) {
  const run = v.run, r = run?.result;
  if (run && (!r || !r.accepted)) {
    const pending = run.status === 'streaming' || run.resultState === 'loading';
    return heading(6) + `<div class="outcome-empty"><span>${pending ? '◌' : '↻'}</span><h3>${pending ? 'Waiting for the proof.' : 'No accepted result.'}</h3><p>${pending ? 'The run earns a win only after the evidence checks out.' : 'The system stopped without claiming a speedup.'}</p>${!pending ? `<code>${esc(r?.state || run.terminal?.state || run.meta.state)}</code>` : ''}</div><div class="scene-footer">${pending ? 'Audit pending' : 'Recorded outcome · acceptance rules preserved'}</div>`;
  }
  const data = r || { medianSpeedup: proof.median_speedup, confidenceInterval95: proof.confidence_interval_95, hiddenCases: 2000, timingPairs: 30, sandboxDeleted: true, receiptSha256: proof.receipt_sha256, sourceSha256: proof.source_sha256 };
  return `<div class="success-heading"><span class="success-check">✓</span>${run ? 'Verified result · recorded' : 'Verified example · recorded'}</div><div class="result-hero"><div class="speed">${fx(data.medianSpeedup)}</div><h2>Faster. With the proof.</h2><p class="interval">95% confidence interval <strong>${data.confidenceInterval95.map(fx).join('–')}</strong></p></div><div class="result-metrics"><div><strong>${data.hiddenCases.toLocaleString()}</strong><span>hidden correctness cases</span></div><div><strong>${data.timingPairs}</strong><span>matched timing pairs</span></div><div><strong>✓</strong><span>${data.sandboxDeleted ? 'sandbox deleted' : 'cleanup unconfirmed'}</span></div></div><div class="result-actions">${run ? link(v, data.optimizedArtifactId, '↓ Optimized code', 'nfw-primary') + `<button type="button" class="nfw-secondary" data-changes aria-expanded="${!!run.changes.open}">${run.changes.open ? 'Hide changes' : 'View changes'}</button>` : '<button type="button" class="nfw-primary" data-download="code">↓ Example optimized.py</button>'}<button type="button" class="nfw-secondary" data-proof aria-expanded="${!!(run || v).proofOpen}">View proof</button></div>${run ? changes(run) : `<details><summary>See example source</summary><pre class="code-preview">${esc(recordedCode)}</pre></details>`}${proofPanel(v, data)}<div class="result-foot">${run ? link(v, data.originalArtifactId, '↓ Original code') : 'Original preserved'}<span>${run ? 'Audited recording · no new benchmark' : 'Development example · no new benchmark'}</span></div>`;
}

export function statusText(v) {
  if (!v.run) return v.playing ? `Example replay · ${phaseNames[v.phase]}` : v.phase ? `Example view · ${phaseNames[v.phase]}` : 'Your workspace';
  return v.run.status === 'streaming' ? 'Playing saved evidence · chat stays open' : v.run.status === 'stopped' ? 'Replay paused · choose a stage or replay again' : v.run.status === 'error' ? 'Replay interrupted · your recording is unchanged' : 'Recording complete · inspect any stage';
}

function nav(v, i, label) {
  const d = v.run?.data;
  const passed = d ? [true, d.strategyIds.length > 0, Object.values(d.workers).some(w => w.candidateId) && !Object.keys(d.workers).some(k => workerState(v.run, k).kind === 'working'), Object.values(d.verdicts).some(x => x.approved), v.run.result?.accepted && d.acceptances.length, false, v.run.result?.accepted && v.run.status === 'done'][i] : i < v.phase;
  return `<button type="button" data-phase="${i}" class="${passed && i !== v.phase ? 'done' : ''}" ${i === v.phase ? 'aria-current="step"' : ''}><span class="step-symbol">${i === v.phase ? '●' : i === 5 ? '↗' : passed ? '✓' : '·'}</span>${label}</button>`;
}

export function render(v, animate = false) {
  const app = v.element, phase = v.phase, active = document.activeElement;
  const focusKey = active && app.contains(active) ? ['data-phase', 'data-speed', 'data-replay', 'data-play', 'data-run', 'data-proof', 'data-changes', 'data-pairs', 'data-follow'].map(a => active.hasAttribute(a) ? `[${a}="${CSS.escape(active.getAttribute(a))}"]` : '').find(Boolean) : '';
  app.dataset.phase = String(phase);
  app.querySelector('.sequence').innerHTML = phaseNames.map((label, i) => nav(v, i, label)).join('');
  const surface = app.querySelector('.stage-surface');
  const changed = surface.dataset.scene !== String(phase);
  surface.dataset.scene = String(phase);
  surface.innerHTML = controls(v) + [start, analyze, workers, verify, race, zoo, result][phase](v);
  surface.classList.toggle('nfw-motion', animate && !matchMedia('(prefers-reduced-motion: reduce)').matches);
  surface.classList.toggle('scene-enter', changed && animate && !matchMedia('(prefers-reduced-motion: reduce)').matches);
  if (phase === 4 && v.run && !v.run.race.playing) {
    surface.querySelectorAll('.runner').forEach(runner => runner.style.animationName = 'none');
  }
  if (phase === 4 && v.run?.race.playing && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const { pair, windowMs } = v.run.race;
    const baseline = surface.querySelector('[data-runner="baseline"]'), candidate = surface.querySelector('[data-runner="candidate"]');
    if (baseline && candidate && pair) {
      const scale = windowMs / Math.max(pair.baselineSeconds, pair.candidateSeconds);
      baseline.style.animationDuration = `${Math.max(35, scale * pair.baselineSeconds)}ms`;
      candidate.style.animationDuration = `${Math.max(35, scale * pair.candidateSeconds)}ms`;
    }
  }
  surface.querySelectorAll('[data-animal]').forEach((animal, i) => animal.style.setProperty('--entry-delay', `${i * 180}ms`));
  if (focusKey) app.querySelector(focusKey)?.focus({ preventScroll: true });
  app.querySelector('.preview-status').textContent = statusText(v);
  app.querySelector('.history .agent .chat-body').textContent = v.run ? stageCopy[phase][1] : phaseNotes[phase];
  app.querySelector('.history').hidden = phase === 0;
  app.querySelector('.suggestions').hidden = phase === 0;
  app.querySelector('.suggestions').innerHTML = questions[phase].map(q => `<button type="button" data-question="${esc(q)}">${q}</button>`).join('');
  app.querySelector('.source-tray').hidden = !(v.sourceOpen || phase === 0);
  app.querySelector('textarea').placeholder = phase === 0 ? v.api === 'up' ? 'Ask about a recording once you choose one…' : 'Tell me what you want changed…' : 'Ask about the work, the code, or the proof…';
  const send = app.querySelector('.send');
  send.textContent = phase === 0 ? v.api === 'up' ? 'Use link ↗' : 'Preview flow ↗' : '↑';
  send.setAttribute('aria-label', phase === 0 ? 'Preview code link' : 'Send message');
}
