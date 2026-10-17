// Templated chat: answers are built from the run's real public data. No model is called.
const fx = (n, d = 3) => `${Number(n).toFixed(d)}×`;
const list = (items) => items.length ? items.join(', ') : 'none recorded';

const TOPICS = [
  ['changes', /what.*chang|code.*chang|\bdiff\b/],
  ['recovery', /fallback|recover|retry|primary unavailable/],
  ['timing', /timing|benchmark|measur|race|matched pairs/],
  ['why_two', /why\b.*\b(two|2|both|multiple|more than one)\b|\btwo workers?\b|\b2 workers?\b/],
  ['ci', /\bci\b|confidence|interval|bootstrap|uncertain/],
  ['cost', /cost|budget|token|price|spend|\$|dollar|money|expens/],
  ['speedup', /speed|faster|fast|median|\bx\b|×|times|improv|result|counts as a win/],
  ['verifier', /verif|approv|review/],
  ['workers', /worker|strateg|plan|agent|candidate|team/],
  ['zoo', /zoo|cheat|attack|adversar|honest controls/],
  ['models', /model|llm|nemotron|glm|deepseek|crusoe|provider/],
  ['checked', /check|correct|hidden|proof|audit|receipt|trust|sandbox|valid|safe|prove|honest/],
];

export function pickTopic(message) {
  const text = String(message).toLowerCase();
  return TOPICS.find(([, pattern]) => pattern.test(text))?.[0] ?? 'help';
}

function acceptance(f) {
  const r = f.result;
  return r.accepted ? r : null;
}

const answers = {
  changes(f) {
    if (!f.result.accepted) return 'There is no accepted optimization to compare in this run. Candidate generation alone does not establish a win.';
    if (f.sourcePreview?.includes('Counter') && /\.update\(/.test(f.sourcePreview)) {
      return 'The selected code counts tokens with Counter.update while reading the lines, then ranks the counts. View changes compares the original target module with the generated solve(data) module; imports and the entry point can differ.';
    }
    return `The selected module is ${f.result.candidateId}. View changes shows its actual line diff against the original target module. Matching tested outputs and the audited timing result are checked separately.`;
  },
  timing(f) {
    if (!f.acceptances.length) return 'No full timing test was recorded in this run. A screen only ranks candidates; it cannot establish an accepted speedup.';
    return `The harness uses ${f.result.timingPairs ?? 'matched'} matched timing pairs on the same machine, executed serially. Each measured output must match. The animated race replays stored receipt measurements; it is not a new benchmark.`;
  },
  recovery(f) {
    const recovered = f.events.filter(e => e.type === 'candidate_created' && e.attempts?.some(a => a.kind === 'fallback' && a.outcome === 'ok'));
    if (!recovered.length) return 'No successful fallback recovery was recorded for this run.';
    const drill = f.events.some(e => e.type === 'fault_injection');
    return `${recovered.map(e => e.worker).join(', ')} recovered through a fallback model and produced a candidate.${drill ? ' This was a labeled fault drill: primary failures were deliberately injected, not a provider outage.' : ' This is recorded model recovery.'} Full acceptance is still required for the selected candidate.`;
  },
  speedup(f) {
    const r = acceptance(f);
    const screens = f.screens.filter((s) => typeof s.screen_median_speedup === 'number')
      .map((s) => `${s.candidate_id} ${fx(s.screen_median_speedup, 2)}`);
    if (r) {
      return `The audited result is a median speedup of ${fx(r.medianSpeedup)} over ${r.timingPairs} matched timing pairs for ${r.candidateId}, `
        + `${f.minimumSpeedup === null ? '' : `against the ${fx(f.minimumSpeedup, 1)} bar. `}Screening estimates (${list(screens)}) were used for ranking only and are not the result.`;
    }
    return `This run (${f.result.state}) has no passing full acceptance, so there is no verified speedup to report. `
      + `Screening estimates, which only rank candidates: ${list(screens)}.`;
  },
  ci(f) {
    const r = acceptance(f);
    if (!r) return `No passing full acceptance was recorded for this run (${f.result.state}), so there is no confidence interval.`;
    return `The 95% bootstrap confidence interval for the median speedup is ${r.confidenceInterval95[0].toFixed(3)}–${r.confidenceInterval95[1].toFixed(3)}× `
      + `over ${r.timingPairs} paired timings (10,000 resamples, fixed seed). ${f.minimumSpeedup !== null && r.confidenceInterval95[0] >= f.minimumSpeedup ? `The whole interval is above the ${fx(f.minimumSpeedup, 1)} bar.` : ''}`;
  },
  verifier(f) {
    if (!f.verdicts.length) return 'No verifier verdict was recorded for this run. Only a hash-bound verifier approval allows a candidate to reach full acceptance.';
    const lines = f.verdicts.map((v) => `${v.candidate_id}: ${v.approved ? 'approved' : v.verifier_failed ? 'no verdict' : 'not approved'}`);
    return `A separate verifier model reviews the exact candidate source and its SHA-256 before full timing. Verdicts: ${lines.join('; ')}. `
      + `Approval is bound to the hash, so changing the source afterwards invalidates it.`;
  },
  workers(f) {
    const pairs = f.orders.map((o) => `${o.worker} → ${o.strategy}`);
    const made = f.candidates.map((c) => c.id);
    return `A planner proposed ${f.strategies.length} strateg${f.strategies.length === 1 ? 'y' : 'ies'} (${list(f.strategies)}). `
      + `Assignments: ${list(pairs)}. Candidates written: ${list(made)}.`
      + (f.terminal?.last_forwarded_candidate_id ? ` Forwarded to the verifier: ${f.terminal.last_forwarded_candidate_id}.` : '');
  },
  why_two(f) {
    return `Two workers try different strategies so one weak idea does not decide the run. The current controller executes them sequentially, and each can fall back to a second model if its first fails. `
      + `This run used ${f.orders.length || 'no'} worker assignment${f.orders.length === 1 ? '' : 's'} (${list(f.orders.map((o) => o.worker))}). `
      + `Both are only candidates: everything still goes through the same screen, verifier and full acceptance.`;
  },
  zoo() {
    return 'The cheating zoo is a separate development defense pilot: 4/4 planted attacks were rejected and 4/4 honest controls passed. Those results are independent of this optimization run; replaying the animal scene does not run the suite again.';
  },
  cost(f) {
    const b = f.budget;
    if (!b) return 'No budget snapshot was recorded for this run.';
    const cost = typeof b.cost_usd === 'number' ? `$${b.cost_usd.toFixed(4)}${b.max_cost_usd ? ` of a $${b.max_cost_usd.toFixed(2)} cap` : ''}` : 'unpriced';
    return `Recorded model spend: ${cost}, ${b.tokens_used ?? '?'} tokens${b.max_tokens ? ` of ${b.max_tokens}` : ''}, `
      + `${b.attempts_used ?? '?'} model attempts${b.max_attempts ? ` of ${b.max_attempts}` : ''}. The run stops if any cap or the deadline is reached.`;
  },
  models(f) {
    const roles = f.models ? Object.entries(f.models).map(([role, m]) => `${role}: ${m.primary} (fallback ${m.fallback})`) : [];
    const used = f.candidates.filter((c) => c.models.length).map((c) => `${c.id} by ${c.models.join(', ')}`);
    return `Configured models: ${list(roles)}. Candidates were actually produced by: ${list(used)}.`;
  },
  checked(f) {
    // Every clause comes from a recorded fact; a stage with no recorded event is reported as not reached.
    const r = f.result;
    const clauses = [`the ledger hash chain ${r.ledgerVerified ? 'verified' : 'did NOT verify'}`];
    const screened = f.screens.filter((s) => s.status === 'screened');
    const correct = f.screens.filter((s) => s.correctness_passed === true);
    clauses.push(f.screens.length
      ? `screening ran for ${f.screens.length} candidate${f.screens.length === 1 ? '' : 's'} (${screened.length} timed); ${correct.length} passed correctness on the ${r.hiddenCases} hidden cases`
      : 'screening: not reached (no candidate was screened)');
    const approved = f.verdicts.filter((v) => v.approved === true);
    clauses.push(!f.verdicts.length ? 'verifier: not reached'
      : approved.length ? `the verifier approved ${approved.map((v) => v.candidate_id).join(', ')} by source hash`
        : 'the verifier did not approve any candidate');
    const decision = f.audit?.valid ? f.audit.decision : null;
    const last = f.acceptances.at(-1);
    const pairs = decision?.pairs ?? last?.acceptance?.pairs;
    clauses.push(!f.acceptances.length ? 'full acceptance: not reached'
      : `full acceptance ran ${f.acceptances.length} time${f.acceptances.length === 1 ? '' : 's'}`
        + `${pairs ? ` with ${pairs} timing pairs` : ''}${r.accepted ? ' and passed' : ' and was not accepted'}`
        + `${decision && f.minimumSpeedup !== null ? ` against a ${fx(f.minimumSpeedup, 1)} bar` : ''}`);
    clauses.push(f.audit === null ? 'no receipt exists to audit'
      : f.audit.valid ? 'the stored receipt re-audited as valid offline' : 'the receipt audit FAILED');
    if (r.sandboxDeleted) clauses.push('the evaluation sandbox was confirmed deleted');
    return `Recorded for this run (${r.state}): ${clauses.join('; ')}. Correctness applies to the tested inputs.`;
  },
  help(f) {
    return `I can answer from this run's recorded data (${f.result.state}). Ask about the speedup, the confidence interval, the verifier, the workers and strategies, `
      + `why two workers, the cheating zoo, cost and budget, the models, or what was checked.`;
  },
};

export function chatReply(message, facts) {
  return answers[pickTopic(message)](facts);
}
