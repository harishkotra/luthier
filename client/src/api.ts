import type {
  ConnectionResult,
  Health,
  Preset,
  ProgressEvent,
  Report,
  ScoringConstants,
} from "./types";
import type { Settings } from "./settings";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(`${response.status} ${response.statusText} -- ${text.slice(0, 300)}`);
  }
  if (!response.ok) {
    const detail = (body as { error?: string; detail?: string }) ?? {};
    throw new Error(detail.error ?? detail.detail ?? `${response.status}`);
  }
  return body as T;
}

export function getHealth(): Promise<Health> {
  return request<Health>("/api/health");
}

export function getScoring(): Promise<ScoringConstants> {
  return request<ScoringConstants>("/api/scoring");
}

export async function getPresets(): Promise<{ presets: Preset[]; repoRootDefault: string | null }> {
  return request("/api/presets");
}

export interface AuditOptions {
  repoPath?: string;
  settings: Settings;
  categories: string[];
  file?: File | null;
}

export async function startAudit(options: AuditOptions): Promise<{ id: string; repo: string }> {
  const payload = {
    baseUrl: options.settings.baseUrl,
    apiKey: options.settings.apiKey,
    model: options.settings.model,
    repoRoot: options.settings.repoRoot,
  };
  if (options.file) {
    const form = new FormData();
    form.append("file", options.file);
    form.append("settings", JSON.stringify(payload));
    form.append("categories", JSON.stringify(options.categories));
    return request<{ id: string; repo: string }>("/api/audit", { method: "POST", body: form });
  }
  return request<{ id: string; repo: string }>("/api/audit", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      repoPath: options.repoPath ?? "",
      settings: payload,
      categories: options.categories,
    }),
  });
}

export function getReport(runId: string): Promise<Report> {
  return request<Report>(`/api/audit/${runId}`);
}

export function testConnection(
  settings: Settings,
): Promise<ConnectionResult> {
  return request<ConnectionResult>("/api/inference/test", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      settings: {
        baseUrl: settings.baseUrl,
        apiKey: settings.apiKey,
        model: settings.model,
      },
    }),
  });
}

export interface EvidencePayload {
  path: string;
  exists: boolean;
  lineCount?: number;
  citedLine?: number;
  citedText?: string | null;
  startLine?: number;
  endLine?: number;
  context: { line: number; text: string }[];
  truncated?: boolean;
  journalRef?: string;
}

export function getEvidence(repo: string, path: string, line: number): Promise<EvidencePayload> {
  const params = new URLSearchParams({ repo, path, line: String(line), ctx: "5" });
  return request<EvidencePayload>(`/api/evidence?${params.toString()}`);
}

/** Subscribe to the SSE stream; returns a closer. */
export function streamAudit(
  runId: string,
  onEvent: (event: ProgressEvent) => void,
  onError: (message: string) => void,
): () => void {
  const source = new EventSource(`/api/audit/${runId}/stream`);
  source.onmessage = (message) => {
    let event: ProgressEvent;
    try {
      event = JSON.parse(message.data) as ProgressEvent;
    } catch {
      return;
    }
    onEvent(event);
    if (event.type === "done" || event.type === "error") {
      if (event.type === "error") onError(event.detail ?? "audit failed");
      source.close();
    }
  };
  source.onerror = () => {
    // The run may already be finished (stream closed server-side); the caller polls
    // /api/audit/:id, so a closed socket is not automatically an error.
    source.close();
  };
  return () => source.close();
}
