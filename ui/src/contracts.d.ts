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

export interface PipelineAdapter {
  start(request: RunRequest, signal: AbortSignal): Promise<{ runId: string }>;
  events(runId: string, signal: AbortSignal): AsyncIterable<PipelineEvent>;
  chat(runId: string, message: string, signal: AbortSignal): Promise<string>;
  cancel(runId: string): Promise<void>;
  result(runId: string): Promise<VerifiedResult>;
  artifact(artifactId: string, signal: AbortSignal): Promise<Blob>;
}
