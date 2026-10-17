// Pure ledger-event -> PipelineEvent mapping. Public fields only, by explicit allowlist.
// Never copies prompts, plan descriptions, candidate source, attempt detail text or error bodies.

export const TERMINAL_PASS = new Set(['verified_done', 'simulation_completed']);

/** Ledger strings can echo model output (strategy ids, labels): strip control chars and clip. */
export function clean(value, limit = 200) {
  if (typeof value !== 'string') return null;
  const text = value.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, ' ').trim();
  return text.length > limit ? text.slice(0, limit) + '…' : text;
}

const num = (value) => (typeof value === 'number' && Number.isFinite(value) ? value : null);
const int = (value) => (Number.isInteger(value) ? value : null);
const bool = (value) => (typeof value === 'boolean' ? value : null);
const sha = (value) => (typeof value === 'string' && /^[0-9a-f]{64}$/.test(value) ? value : null);
const id = (value) => (typeof value === 'string' && /^[A-Za-z0-9._-]{1,64}$/.test(value) ? value : null);

function attempts(list) {
  if (!Array.isArray(list)) return [];
  return list.slice(0, 12).map((item) => ({
    kind: clean(item?.kind, 24), outcome: clean(item?.outcome, 32), model_id: clean(item?.model_id, 96),
  }));
}

function models(value) {
  if (!value || typeof value !== 'object') return null;
  const result = {};
  for (const role of ['planner', 'worker_a', 'worker_b', 'verifier']) {
    if (value[role]) result[role] = { primary: clean(value[role].primary, 96), fallback: clean(value[role].fallback, 96) };
  }
  return result;
}

function acceptance(value) {
  if (!value || typeof value !== 'object') return null;
  return {
    pairs: int(value.pairs), median_speedup: num(value.median_speedup),
    ci_lower: num(value.ci_lower), ci_upper: num(value.ci_upper),
  };
}

const x = (value, digits = 2) => `${value.toFixed(digits)}×`;

/** @returns {object|null} PipelineEvent, or null when the ledger event is not shown. */
export function mapEvent(runId, sequence, event) {
  const type = event?.type;
  const make = (stage, status, message, detail) => ({
    runId, sequence, stage, status, message, kind: type, detail,
  });
  const candidate = id(event.candidate_id) ?? 'candidate';
  const worker = id(event.worker) ?? 'worker';
  switch (type) {
    case 'run_started': {
      const rounds = int(event.max_rounds);
      return make('analyze', 'running', `Run started${rounds ? `, up to ${rounds} rounds` : ''}`, {
        session_id: clean(event.session_id, 128), models: models(event.models),
        max_rounds: rounds, deadline_seconds: num(event.deadline_seconds),
      });
    }
    case 'run_label':
      return make('analyze', 'running', `Label: ${clean(event.label) ?? 'unlabeled'}`, { label: clean(event.label) });
    case 'fault_injection':
      return make('analyze', 'running', `Fault drill: ${clean(event.label) ?? 'labeled drill'}`, { label: clean(event.label) });
    case 'simulation_label':
      return make('analyze', 'running', `Simulation: ${clean(event.label) ?? 'labeled simulation'}`, { label: clean(event.label) });
    case 'preflight':
      return make('analyze', 'running', event.ok === true ? 'Preflight checks passed' : 'Preflight checks failed', {});
    case 'plan': {
      const ids = (Array.isArray(event.strategies) ? event.strategies : [])
        .map((item) => clean(item?.strategy_id, 64)).filter(Boolean).slice(0, 8);
      return make('analyze', 'running', `Planner proposed ${ids.length} ${ids.length === 1 ? 'strategy' : 'strategies'}`, { strategy_ids: ids });
    }
    case 'worker_order':
      return make('workers', 'running', `${worker} assigned strategy ${clean(event.strategy_id, 64) ?? '?'}`, {
        worker, strategy_id: clean(event.strategy_id, 64),
        primary: clean(event.primary, 96), fallback: clean(event.fallback, 96),
      });
    case 'candidate_created':
      return make('workers', 'running', `${worker} wrote candidate ${candidate}`, {
        worker, candidate_id: id(event.candidate_id), candidate_sha256: sha(event.candidate_sha256),
        strategy_id: clean(event.strategy_id, 64), attempts: attempts(event.attempts),
      });
    case 'worker_failed':
      return make('workers', 'running', `${worker} failed to produce a candidate`, {
        worker, strategy_id: clean(event.strategy_id, 64), attempts: attempts(event.attempts),
      });
    case 'duplicate_candidate':
      return make('workers', 'running', `${worker} produced a duplicate candidate (skipped)`, {
        worker, candidate_id: id(event.candidate_id), candidate_sha256: sha(event.candidate_sha256),
      });
    case 'screen_result': {
      const passed = event.correctness_passed === true && event.status === 'screened';
      const speedup = num(event.screen_median_speedup);
      const timing = speedup === null ? 'no timing' : `${x(speedup)} (ranking only)`;
      const text = event.status === 'screened'
        ? `Screen ${candidate}: ${timing}, correctness ${event.correctness_passed === true ? 'passed' : 'failed'}`
        : `Screen ${candidate}: ${clean(event.status, 40) ?? 'unknown status'}, correctness ${event.correctness_passed === true ? 'passed' : 'failed'}`;
      return make('verify', passed ? 'passed' : 'failed', text, {
        candidate_id: id(event.candidate_id), status: clean(event.status, 40),
        correctness_passed: bool(event.correctness_passed), screen_median_speedup: speedup,
      });
    }
    case 'verifier_verdict': {
      const approved = event.approved === true;
      let text = `Verifier approved ${candidate} (hash-bound)`;
      if (!approved) {
        text = event.verifier_failed === true ? `Verifier produced no verdict for ${candidate}`
          : event.mismatch === true ? `Verifier rejected ${candidate} (source mismatch)`
            : `Verifier did not approve ${candidate}`;
      }
      return make('verify', approved ? 'passed' : 'failed', text, {
        candidate_id: id(event.candidate_id), approved, mismatch: bool(event.mismatch),
      });
    }
    case 'acceptance_result': {
      const accepted = event.accepted === true;
      const a = acceptance(event.acceptance);
      let text;
      if (a && a.median_speedup !== null) {
        const ci = a.ci_lower !== null && a.ci_upper !== null ? `, 95% CI ${a.ci_lower.toFixed(3)}–${a.ci_upper.toFixed(3)}×` : '';
        const pairs = a.pairs !== null ? ` over ${a.pairs} pairs` : '';
        text = `Full acceptance${accepted ? '' : ' not met'}: median ${a.median_speedup.toFixed(3)}×${ci}${pairs}`;
      } else {
        text = `Full acceptance ${accepted ? 'passed' : 'not met'} (${clean(event.status, 40) ?? 'no timing'})`;
      }
      return make('race', accepted ? 'passed' : 'failed', text, {
        candidate_id: id(event.candidate_id), status: clean(event.status, 40), accepted,
        origin: clean(event.origin, 24), receipt_sha256: sha(event.receipt_sha256), acceptance: a,
      });
    }
    case 'terminal': {
      const state = clean(event.state, 48) ?? 'unknown';
      const reason = clean(event.reason, 200);
      return make('result', TERMINAL_PASS.has(state) ? 'passed' : 'failed',
        TERMINAL_PASS.has(state) || !reason ? state : `${state}: ${reason}`, {
          state, reason, last_forwarded_candidate_id: id(event.last_forwarded_candidate_id),
        });
    }
    case 'harness_error':
    case 'evidence_invalid':
    case 'harness_returned_late': {
      const mode = event.mode === 'acceptance' ? 'acceptance' : 'screen';
      const text = { harness_error: 'Harness error', evidence_invalid: 'Evidence invalid', harness_returned_late: 'Harness returned late' }[type];
      return make(mode === 'acceptance' ? 'race' : 'verify', 'running', `${text} for ${candidate} (${mode})`, {
        candidate_id: id(event.candidate_id), mode,
      });
    }
    default:
      return null; // heartbeat and unknown event types are not shown
  }
}
