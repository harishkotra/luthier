import type { Finding, Severity } from "./types";

export const SEVERITY_CLASS: Record<Severity, string> = {
  critical: "border-crack/60 bg-crack/15 text-[#f08c80]",
  high: "border-crack/40 bg-crack/10 text-[#e0897d]",
  medium: "border-warp/50 bg-warp/12 text-[#e3bb6a]",
  low: "border-spruce/50 bg-spruce/12 text-[#7fc2ab]",
  info: "border-bench-600 bg-bench-800 text-bench-500",
};

export const SEVERITY_DOT: Record<Severity, string> = {
  critical: "bg-crack",
  high: "bg-[#e0897d]",
  medium: "bg-warp",
  low: "bg-spruce",
  info: "bg-bench-500",
};

const ORDER: Severity[] = ["critical", "high", "medium", "low", "info"];

export function severityRank(finding: Finding): number {
  return ORDER.indexOf(finding.severity);
}

export function scoreTone(score: number): { text: string; ring: string; word: string } {
  if (score >= 95)
    return { text: "text-[#7fc2ab]", ring: "ring-cured/40", word: "in tune" };
  if (score >= 85) return { text: "text-[#a8c98a]", ring: "ring-cured/25", word: "nearly" };
  if (score >= 70) return { text: "text-[#e3bb6a]", ring: "ring-warp/40", word: "drifting" };
  if (score >= 50) return { text: "text-[#eda06a]", ring: "ring-warp/50", word: "out of tune" };
  return { text: "text-[#f08c80]", ring: "ring-crack/50", word: "unplayable" };
}

export function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}

export function formatTime(epochSeconds?: number): string {
  if (!epochSeconds) return "—";
  const date = new Date(epochSeconds * 1000);
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function clip(value: string, limit: number): string {
  const flat = value.replace(/\s+/g, " ").trim();
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 1)}…`;
}
