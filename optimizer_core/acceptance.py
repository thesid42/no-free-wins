"""Only full, measured, evidence-backed acceptance can return accepted=True."""

from dataclasses import asdict, dataclass
import math
import random
import re
from statistics import median

SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class AcceptancePolicy:
    pairs: int = 30
    minimum_speedup: float = 3.0
    bootstrap_resamples: int = 10_000
    bootstrap_seed: int = 42
    ci_level: float = 0.95

    def validate(self):
        if type(self.pairs) is not int or self.pairs != 30:
            raise ValueError("full acceptance requires exactly 30 pairs")
        if not _positive(self.minimum_speedup) or self.minimum_speedup != 3.0:
            raise ValueError("the frozen speed bar is 3.0x")
        if type(self.bootstrap_resamples) is not int or self.bootstrap_resamples != 10_000:
            raise ValueError("full acceptance requires 10000 bootstrap resamples")
        if type(self.bootstrap_seed) is not int:
            raise ValueError("invalid bootstrap seed")
        if type(self.ci_level) not in (int, float) or self.ci_level != 0.95:
            raise ValueError("full acceptance requires a 95% CI")


@dataclass(frozen=True)
class TimingPair:
    input_sha256: str
    baseline_seconds: float
    candidate_seconds: float
    baseline_output_sha256: str
    candidate_output_sha256: str
    environment_sha256: str


def _percentile(ordered, q):
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def evaluate(pairs, *, origin, correctness_passed, verifier_passed,
             evidence_complete, policy=None):
    policy = policy or AcceptancePolicy()
    policy.validate()
    if origin not in ("measured", "synthetic"):
        raise ValueError("unknown measurement origin")
    gates = (correctness_passed, verifier_passed, evidence_complete)
    if any(type(value) is not bool for value in gates):
        raise ValueError("gate values must be booleans")
    if len(pairs) != policy.pairs:
        raise ValueError("screening or incomplete trials cannot issue acceptance")
    inputs, environments, ratios = set(), set(), []
    outputs_match = True
    for pair in pairs:
        for field in (pair.input_sha256, pair.baseline_output_sha256,
                      pair.candidate_output_sha256, pair.environment_sha256):
            if type(field) is not str or not SHA256.fullmatch(field):
                raise ValueError("invalid artifact digest")
        if pair.input_sha256 in inputs:
            raise ValueError("acceptance pairs must use distinct inputs")
        inputs.add(pair.input_sha256)
        environments.add(pair.environment_sha256)
        if not _positive(pair.baseline_seconds) or not _positive(pair.candidate_seconds):
            raise ValueError("timings must be positive finite numbers")
        ratio = pair.baseline_seconds / pair.candidate_seconds
        if not math.isfinite(ratio):
            raise ValueError("speedup is not finite")
        ratios.append(ratio)
        outputs_match &= pair.baseline_output_sha256 == pair.candidate_output_sha256
    if len(environments) != 1:
        raise ValueError("mixed measurement environments")
    rng = random.Random(policy.bootstrap_seed)
    samples = sorted(median(rng.choices(ratios, k=len(ratios)))
                     for _ in range(policy.bootstrap_resamples))
    alpha = (1 - policy.ci_level) / 2
    lower, upper = _percentile(samples, alpha), _percentile(samples, 1 - alpha)
    observed = median(ratios)
    reasons = []
    for condition, reason in (
        (origin == "measured", "synthetic_evidence"),
        (correctness_passed, "correctness_gate_failed"),
        (verifier_passed, "verifier_gate_failed"),
        (evidence_complete, "incomplete_evidence"),
        (outputs_match, "timed_output_mismatch"),
        (observed >= policy.minimum_speedup, "median_below_bar"),
        (lower >= policy.minimum_speedup, "ci_lower_bound_below_bar"),
    ):
        if not condition:
            reasons.append(reason)
    return {"accepted": not reasons, "origin": origin, "pairs": len(pairs),
            "median_speedup": observed, "ci_lower": lower, "ci_upper": upper,
            "reasons": reasons, "policy": asdict(policy)}
