// Pure presentation helpers. A replay never invents a measurement or turns a screen into acceptance.
export const stageCopy = [
  ['Your next verified win.', 'Choose a recording, or explore the example.'],
  ['Finding repeated work.', 'Understand the function. Keep its contract intact.'],
  ['Two strategies. One contract.', 'Different approaches, independently checked.'],
  ['Checking the winner.', 'Correctness first. Independent review next.'],
  ['Proving the speedup.', 'Matched inputs. Matching outputs. Measured times.'],
  ['No shortcuts through the zoo.', 'A separate defense demo with planted attacks and honest controls.'],
  ['A win you can inspect.', 'The code, the measurement, and the evidence.'],
];

export const questions = [
  [], ['What is being checked?', 'Why two workers?'],
  ['Explain the strategies', 'Did fallback recover?'],
  ['What does the verifier check?', 'Why was this candidate selected?'],
  ['How is timing measured?', 'What counts as a win?'],
  ['What does the zoo test?', 'Did honest controls pass?'],
  ['What changed in the code?', 'Explain the proof'],
];

export function strategyLabel(value) {
  return String(value || 'Strategy pending').replaceAll('_', ' ').replaceAll('-', ' ');
}

export function snippet(source) {
  if (!source) return '';
  const lines = source.split(/\r?\n/);
  const start = lines.findIndex(line => /^\s*(async )?def solve\(/.test(line));
  const selected = lines.slice(start < 0 ? 0 : start);
  let quote = null;
  const body = selected.filter((line, index) => {
    const text = line.trim();
    if (!text || text.startsWith('#')) return false;
    if (quote) { if (text.includes(quote)) quote = null; return false; }
    if (index > 0 && /^("""|''')/.test(text)) {
      const marker = text.slice(0, 3);
      if (!text.slice(3).includes(marker)) quote = marker;
      return false;
    }
    return true;
  });
  const operations = body.slice(1).filter(line => /\bfor\b|\breturn\b|Counter|\.update\(/.test(line));
  return operations.length >= 3 ? [body[0], ...operations.slice(0, 4)].join('\n') : body.slice(0, 5).join('\n');
}

export function workerState(run, key) {
  const worker = run.data.workers[key];
  if (worker?.visualAttempt) {
    const a = worker.visualAttempt;
    if (a.outcome !== 'ok') return { kind: 'recovering', label: a.kind === 'retry' ? 'Retry unavailable' : 'Primary unavailable' };
    return { kind: 'recovering', label: a.kind === 'fallback' ? 'Fallback picked up the task' : 'Generating candidate' };
  }
  if (worker?.failed) return { kind: 'failed', label: 'No candidate produced' };
  if (worker?.candidateId) return { kind: 'complete', label: 'Candidate ready' };
  if (run.status !== 'streaming') return { kind: 'pending', label: 'No candidate recorded' };
  const keys = Object.keys(run.data.workers);
  const active = keys.find(k => !run.data.workers[k].candidateId && !run.data.workers[k].failed);
  return key === active ? { kind: 'working', label: 'Exploring this strategy' } : { kind: 'pending', label: 'Queued' };
}

export function milestone(stage) {
  return {
    analyze: 'I’m checking the contract and choosing two approaches.',
    workers: 'The workers are trying different strategies. You can keep chatting here.',
    verify: 'The candidates are being checked against the original. The verifier reviews the selected code.',
    race: 'The timing stage is ready. Any pair animation here replays stored measurements.',
    result: 'The replay is complete. Open the result to inspect its code and proof.',
  }[stage] || '';
}

/** Bounded line diff for the viewer. Full source remains available through the existing downloads. */
export function lineDiff(original, optimized, limit = 200) {
  const a = String(original).split(/\r?\n/).slice(0, limit);
  const b = String(optimized).split(/\r?\n/).slice(0, limit);
  const table = Array.from({ length: a.length + 1 }, () => new Uint16Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      table[i][j] = a[i] === b[j] ? 1 + table[i + 1][j + 1] : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0, j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) rows.push({ kind: 'same', text: a[i++], oldLine: i, newLine: ++j });
    else if (j < b.length && (i === a.length || table[i][j + 1] >= table[i + 1][j])) rows.push({ kind: 'added', text: b[j++], oldLine: null, newLine: j });
    else rows.push({ kind: 'removed', text: a[i++], oldLine: i, newLine: null });
  }
  return { rows, truncated: String(original).split(/\r?\n/).length > limit || String(optimized).split(/\r?\n/).length > limit };
}
