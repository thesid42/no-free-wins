// Local UI controller only. No API client, model calls, source fetching, or sandbox execution.
// Future controller-facing types live in contracts.d.ts; they are intentionally unimplemented.
import { codeBytes, proof, phaseNames, phaseNotes } from './demo-data.js';
import { render } from './scenes.js';

const app = document.querySelector('[data-app="sidecar"]');
const input = app.querySelector('textarea');
const source = app.querySelector('input');
const error = app.querySelector('.source-error');
const conversation = app.querySelector('.conversation-scroll');
const messages = app.querySelector('.chat-extra');
const view = {
  element: app,
  phase: 0,
  sourceOpen: true,
  playing: false,
  timer: null,
  options: { renderer: 'Line graph' },
};

function stop() {
  clearTimeout(view.timer);
  view.timer = null;
  view.playing = false;
}

function go(phase, animate = false) {
  view.phase = Math.max(0, Math.min(phaseNames.length - 1, phase));
  if (view.phase > 0) view.sourceOpen = false;
  render(view, animate);
}

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

function addMessage(role, text) {
  const article = document.createElement('article');
  article.className = `message ${role}`;
  if (role === 'agent') {
    const speaker = document.createElement('div');
    speaker.className = 'speaker';
    speaker.textContent = '✳ No Free Wins · demo';
    article.append(speaker);
  }
  const body = document.createElement('p');
  body.className = 'chat-body';
  body.textContent = text;
  article.append(body);
  messages.append(article);
  conversation.scrollTop = conversation.scrollHeight;
}

function chat(text) {
  if (!text.trim()) return;
  addMessage('user', text);
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
  view.sourceOpen = false;
  error.hidden = true;
  source.removeAttribute('aria-invalid');
  app.querySelector('.history .attachment').textContent = '↗ Example · parser.py';
  app.querySelector('.history .user .chat-body').textContent = 'Make this parser faster. Keep the output exactly the same.';
  play();
}

function submitInput() {
  if (view.phase !== 0) {
    chat(input.value);
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
    stop();
    go(Number(button.dataset.phase), true);
    app.querySelector(`button[data-phase="${view.phase}"]`).focus({ preventScroll: true });
  } else if (button.hasAttribute('data-play')) {
    if (view.playing) {
      stop();
      render(view, false);
    } else startExample();
  } else if (button.hasAttribute('data-example')) {
    startExample();
  } else if (button.hasAttribute('data-renderer')) {
    view.options.renderer = view.options.renderer === 'ASCII' ? 'Line graph' : 'ASCII';
    render(view, false);
    app.querySelector('[data-renderer]').focus({ preventScroll: true });
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
    view.sourceOpen = true;
    messages.replaceChildren();
    source.value = '';
    input.value = '';
    error.hidden = true;
    source.removeAttribute('aria-invalid');
    go(0);
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
window.addEventListener('pagehide', stop);
render(view, false);
