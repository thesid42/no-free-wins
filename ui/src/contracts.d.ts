/** Controller-facing types. The launch types below mirror the bridge's job API (adapter.js). */
export type Stage = 'analyze' | 'workers' | 'verify' | 'race' | 'zoo' | 'result';

export interface PipelineEvent {
  runId: string;
  sequence: number;
  stage: Stage;
  status: 'pending' | 'running' | 'passed' | 'failed' | 'cancelled';
  message: string;
  /** Replay bridge only: raw ledger event type and its public fields. */
  kind?: string;
  detail?: Record<string, unknown>;
}

/** Replay bridge: one entry of GET /api/runs. */
export interface RunSummary {
  runId: string;
  sessionId: string;
  kind: 'official' | 'repo' | 'development';
  label: string;
  state: string;
  startedUtc: string | null;
  endedUtc: string | null;
  taskId: string;
  /** true only when the bridge's Python helper verified the ledger; null while not yet checked. */
  verified: boolean | null;
}

export interface VerifiedResult {
  runId: string;
  accepted: boolean;
  medianSpeedup: number;
  confidenceInterval95: [number, number];
  timingPairs: number;
  hiddenCases: number;
  sourceSha256: string;
  receiptSha256: string;
  receiptAuditValid: boolean;
  sandboxDeleted: boolean;
  // Artifact handles must come from the controller; never turn an arbitrary pasted URL into these.
  originalArtifactId: string;
  optimizedArtifactId: string;
  proofArtifactId: string;
}

/** Replay bridge: GET /api/runs/:runId/result. Speedup fields are only meaningful when accepted. */
export interface ReplayResult extends VerifiedResult {
  state: string;
  sessionId: string;
  label: string | null;
  ledgerVerified: boolean;
  ledgerFinalSha256: string | null;
  taskId: string;
  candidateId: string | null;
}

/** Verified excerpts and receipt measurements used only to animate a recorded replay. */
export interface ReplayPresentation {
  candidates: Record<string, string>;
  pairTimings: Array<{ baselineSeconds: number; candidateSeconds: number; outputsMatch: true }>;
}

/** GET /api/health. `launch` is true only when the bridge can start analyses and runs. */
export interface Health {
  ok: boolean;
  mode: 'replay' | 'replay+launch';
  launch?: boolean;
}

export type JobKind = 'analyze' | 'answer' | 'approve';
export type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

export interface AnalystProposal {
  target: string; // path:function
  description: string;
  hints: string[];
  risks: string[];
  generator_source: string;
  include: unknown;
  sizes: { timing_size: number; correctness_sizes: number[] };
}

export interface AnalysisResult {
  proposal_id: string;
  status: 'proposal' | 'questions' | 'needs_input' | 'not_feasible';
  questions: string[];
  suggestions: string[];
  reason: string;
  proposal?: AnalystProposal;
  target_source_excerpt?: string;
}

/** GET /api/jobs/:jobId. For `approve`, `result` is the run summary and is never shown as a claim. */
export interface Job {
  jobId: string;
  kind: JobKind;
  status: JobStatus;
  phase: string | null;
  result: AnalysisResult | Record<string, unknown> | null;
  error: string | null;
  runId?: string | null;
  sessionId?: string | null;
  startedUtc: string | null;
  endedUtc: string | null;
}

/** Every launch call sends `confirmSpend: true`; callers must only invoke them after an explicit user confirmation. */
/** GET /api/zoo (available: true). Separate defense suite; not part of any optimization run. */
export interface ZooReport {
  available: true;
  id: string;
  measuredUtc: string;
  surface: string | null;
  sandboxDeleted: boolean;
  attackCount: number;
  attacksCaught: number;
  honestCount: number;
  honestPassed: number;
  rows: { fixture: string; label: 'attack' | 'honest'; category: string; rejected: boolean; sourceSha256: string | null; proofSha256: string | null }[];
}

export interface PipelineAdapter {
  health(signal?: AbortSignal): Promise<Health>;
  runs(signal?: AbortSignal): Promise<RunSummary[]>;
  events(runId: string, signal: AbortSignal, options?: { speed?: 0 | 1 | 8; onEnd?: (state?: string) => void }): AsyncIterable<PipelineEvent>;
  result(runId: string, signal?: AbortSignal): Promise<ReplayResult>;
  chat(runId: string, message: string, signal?: AbortSignal): Promise<string>;
  artifactUrl(artifactId: string): string;
  presentation(runId: string, signal?: AbortSignal): Promise<ReplayPresentation>;
  /** Newest cheating-zoo report, or null when none is available. */
  zoo(signal?: AbortSignal): Promise<ZooReport | null>;
  source(artifactId: string, signal?: AbortSignal): Promise<string>;
  analyze(repoUrl: string, prompt: string, signal?: AbortSignal): Promise<{ jobId: string }>;
  auto(repoUrl: string, prompt: string, signal?: AbortSignal): Promise<{ jobId: string; sessionPrefix?: string }>;
  answer(proposalId: string, text: string, signal?: AbortSignal): Promise<{ jobId: string }>;
  approve(proposalId: string, signal?: AbortSignal): Promise<{ jobId: string; sessionId?: string }>;
  job(jobId: string, signal?: AbortSignal): Promise<Job>;
  cancel(jobId: string, signal?: AbortSignal): Promise<unknown>;
  pollJob(jobId: string, signal: AbortSignal, onUpdate?: (job: Job) => void): Promise<Job | null>;
}
