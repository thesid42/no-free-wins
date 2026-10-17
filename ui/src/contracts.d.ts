/** Future integration boundary only. Nothing implements or calls this interface yet. */
export type Stage = 'analyze' | 'workers' | 'verify' | 'race' | 'zoo' | 'result';

export interface RunRequest {
  sourceUrl: string;
  instruction: string;
}

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

export interface PipelineAdapter {
  start(request: RunRequest, signal: AbortSignal): Promise<{ runId: string }>;
  events(runId: string, signal: AbortSignal): AsyncIterable<PipelineEvent>;
  chat(runId: string, message: string, signal: AbortSignal): Promise<string>;
  cancel(runId: string): Promise<void>;
  result(runId: string): Promise<VerifiedResult>;
  artifact(artifactId: string, signal: AbortSignal): Promise<Blob>;
}
