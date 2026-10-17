// Pure view-model helpers behind the autonomous timeline and outcome cards (no DOM needed).
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import { autoOutcome, timelineModel } from '../src/scenes.js';

const ev = (step, status, attempt = null, message = `${step} ${status}`) => ({ step, status, attempt, message });

describe('timelineModel', () => {
  test('one row per step in order: analyze, repair, prepare, run 1, lessons, run 2, done', () => {
    const rows = timelineModel([
      ev('analyze', 'started'), ev('analyze', 'ok'),
      ev('prepare', 'started', 1), ev('prepare', 'failed', 1),
      ev('repair', 'started', 1), ev('repair', 'ok', 1),
      ev('prepare', 'started', 2), ev('prepare', 'ok', 2),
      ev('run', 'started', 1), ev('run', 'ok', 1),
      ev('lessons', 'started'), ev('lessons', 'ok'),
      ev('run', 'started', 2), ev('run', 'started', 2, 'still going'),
    ]);
    assert.deepEqual(rows.map((r) => [r.label, r.status]), [
      ['Analyze', 'ok'], ['Prepare sandbox', 'failed'], ['Repair #1', 'ok'], ['Prepare sandbox · try 2', 'ok'],
      ['Run 1', 'ok'], ['Lessons learned', 'ok'], ['Run 2', 'started'],
    ]);
    assert.equal(rows.at(-1).message, 'still going');
  });
  test('a repeated step without attempt numbers starts a new row once the earlier one finished', () => {
    const rows = timelineModel([ev('prepare', 'started'), ev('prepare', 'failed'), ev('prepare', 'started')]);
    assert.deepEqual(rows.map((r) => r.status), ['failed', 'started']);
  });
  test('junk entries are skipped, including prototype-named statuses', () => {
    const rows = timelineModel([null, 7, 'x', {}, { step: 'run' }, { step: 'run', status: 'constructor' }, { step: 'constructor', status: 'ok' }, ev('done', 'ok')]);
    assert.deepEqual(rows.map((r) => r.label), ['constructor', 'Done']);
    assert.deepEqual(timelineModel(undefined), []);
  });
});

describe('autoOutcome', () => {
  test('normalizes gave_up without carrying any speedup', () => {
    const o = autoOutcome({
      status: 'gave_up', report: ' tried twice ', assumptions: ['a', '', 7, ' b '], attempts: [{ step: 'run', state: 'rounds_exhausted' }, 'text', null],
      runs: [{ state: 'rounds_exhausted', run_id: 'r1' }, { state: 'rounds_exhausted', median_speedup: 9.9 }], total_cost_usd: 3,
    });
    assert.equal(o.status, 'gave_up');
    assert.equal(o.report, 'tried twice');
    assert.deepEqual(o.assumptions, ['a', 'b']);
    assert.deepEqual(o.attempts, ['step run · state rounds_exhausted', 'text']);
    assert.equal(o.lastState, 'rounds_exhausted');
    assert.equal(o.cost, 3);
    assert.ok(!JSON.stringify(o).includes('9.9'));
  });
  test('not_feasible falls back to the report as the reason', () => {
    assert.equal(autoOutcome({ status: 'not_feasible', report: 'I/O bound' }).reason, 'I/O bound');
    assert.equal(autoOutcome({ status: 'not_feasible', report: 'I/O bound' }).report, '');
    assert.equal(autoOutcome({ status: 'not_feasible', reason: 'why', report: 'more' }).report, 'more');
  });
  test('garbage input is harmless', () => {
    for (const input of [null, undefined, 3, 'x', [], { runs: 'no', attempts: 4, assumptions: {} }]) {
      const o = autoOutcome(input);
      assert.equal(o.status, '');
      assert.deepEqual(o.runs, []);
      assert.equal(o.cost, null);
    }
  });
});
