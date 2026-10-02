export type Severity = "critical" | "high" | "medium" | "low" | "info";

export type Category = "paths" | "commands" | "skills" | "mcp" | "conventions";

export interface EvidenceRow {
  line: number;
  text: string;
}

export interface RealityEvidence {
  path: string;
  line: number;
  text: string;
  quote: string;
  ref: string;
  label?: string;
}

export interface Evidence {
  path: string;
  line: number;
  text: string;
  quote: string;
  context: EvidenceRow[];
  journalRef: string;
  reality?: RealityEvidence | null;
  unverifiedCitation?: string;
}

export interface Finding {
  id: string;
  category: Category;
  check: string;
  checkLabel: string;
  severity: Severity;
  title: string;
  message: string;
  ruleText: string;
  ruleFile: string;
  ruleLine: number;
  reality: string;
  evidence: Evidence;
  suggestion: string;
  diff: string | null;
  deterministic: boolean;
  verdict: string | null;
  engine: string;
  advisory: boolean;
  skipped: boolean;
  occurrences: string[];
  journalRef: string;
  folded: number;
}

export interface Rule {
  id: string;
  source: string;
  line: number;
  kind: string;
  text: string;
  check: string;
  checkLabel: string;
  target: string;
  extractor: string;
  occurrences: string[];
}

export interface CategoryLedger {
  label: string;
  count: number;
  rawPenalty: number;
  appliedPenalty: number;
  capped: boolean;
  bySeverity: Record<Severity, number>;
  deterministic: number;
  semantic: number;
  unknown: number;
  advisory: number;
}

export interface ScoringConstants {
  baseScore: number;
  floorScore: number;
  penalties: {
    deterministicFailure: number;
    semanticViolation: number;
    semanticUnknown: number;
  };
  maxPenaltyPerCategory: number;
  maxFindingsPerCheck: number;
  advisoryPenalty: number;
  categories: Category[];
  categoryLabels: Record<Category, string>;
  severities: Severity[];
  formula: string;
}

export interface Scoring {
  score: number;
  base: number;
  totalRawPenalty: number;
  totalAppliedPenalty: number;
  perCategory: Record<string, CategoryLedger>;
  counts: Record<string, number>;
  deterministicFailures: number;
  semanticViolations: number;
  unknowns: number;
  skipped: number;
  breakdown: { findingId: string; category: string; kind: string; penalty: number }[];
  constants: ScoringConstants;
}

export interface SourceProvenance {
  kind: "path" | "zip" | "url";
  url?: string;
  path?: string;
  commit?: string | null;
  ref?: string | null;
  refRequested?: string | null;
  bytes?: number;
  seconds?: number;
  file?: string;
  requested?: string;
}

export interface Report {
  runId: string;
  source?: SourceProvenance;
  repo: {
    path: string;
    files: number;
    harnessFiles: string[];
    mcpConfigs: string[];
    skillDirs: string[];
    manifests: {
      packageJson: string[];
      scripts: string[];
      makeTargets: string[];
      pyproject: string[];
      requirements: string[];
      deps: string[];
    };
  };
  extraction: {
    static: number;
    llmAccepted: number;
    rejected: { source?: string; line?: number; text?: string; reason: string }[];
    rejectedCount: number;
    total: number;
    byKind: Record<string, number>;
  };
  rules: Rule[];
  findings: Finding[];
  skipped: { stage: string; reason: string; detail?: string; rules?: number }[];
  score: number;
  scoring: Scoring;
  constants: ScoringConstants;
  toolCalls: {
    total: number;
    bytesReturned: number;
    byTool: Record<string, { calls: number; bytes: number; errors: number }>;
    logPath: string | null;
  };
  inference?: {
    baseUrl: string;
    model: string;
    keyConfigured: boolean;
    keyPreview: string;
    used: boolean;
  };
  agentNarrative?: { status: string; text: string | null; toolCalls: number };
  startedAt?: number;
  finishedAt?: number;
  durationSeconds?: number;
  error?: string;
}

export interface ProgressEvent {
  type: "hello" | "progress" | "status" | "done" | "error";
  runId?: string;
  stage?: string;
  status?: string;
  detail?: string;
  score?: number;
  findings?: number;
  files?: string[];
}

export interface Preset {
  id: string;
  label: string;
  default?: boolean;
  baseUrl: string;
  model: string;
  needsKey: boolean;
  keyEnv?: string;
  baseUrlEnv?: string;
}

export interface Health {
  ok: boolean;
  service: string;
  version: string;
  repoRootDefault: string | null;
  inferenceConfigured: boolean;
  baseUrl: string;
  model: string;
  keyPreview: string;
  auditLog: string;
  maxBytesPerFile: number;
}

export interface ConnectionResult {
  ok: boolean;
  baseUrl: string;
  model: string;
  keyConfigured: boolean;
  keyPreview: string;
  error: string | null;
  steps: {
    method: string;
    url: string;
    status: number | null;
    latencyMs: number | null;
    body: string;
    error: string | null;
  }[];
}
