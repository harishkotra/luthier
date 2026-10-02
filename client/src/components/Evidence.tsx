import { useEffect, useState } from "react";
import { getEvidence, type EvidencePayload } from "../api";
import type { Finding } from "../types";

interface Props {
  finding: Finding | null;
  repo: string;
}

export default function Evidence({ finding, repo }: Props) {
  const [rule, setRule] = useState<EvidencePayload | null>(null);
  const [code, setCode] = useState<EvidencePayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const reality = finding?.evidence?.reality ?? null;

  useEffect(() => {
    if (!finding || !repo) {
      setRule(null);
      setCode(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setCode(null);
    (async () => {
      try {
        const first = await getEvidence(repo, finding.evidence.path, finding.evidence.line);
        if (cancelled) return;
        setRule(first);
      } catch (err) {
        if (!cancelled) setError(String((err as Error).message ?? err));
      }
      if (reality && !cancelled) {
        try {
          const second = await getEvidence(repo, reality.path, reality.line);
          if (!cancelled) setCode(second);
        } catch {
          /* the cited code line is supplementary; the rule citation still stands */
        }
      }
      if (!cancelled) setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finding?.id, repo]);

  if (!finding) {
    return (
      <section className="panel">
        <div className="panel-head">
          <h2 className="text-[13px] text-varnish">Evidence</h2>
        </div>
        <p className="px-4 py-8 text-center text-[12px] text-bench-500">
          Select a finding to re-read the exact file and line it cites from disk.
        </p>
      </section>
    );
  }

  const snapshot = finding?.evidence?.context ?? [];
  const usingSnapshot = Boolean(error) && snapshot.length > 0;

  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="text-[13px] text-varnish">Evidence</h2>
        <span className="text-[10.5px] text-bench-500">
          {loading
            ? "reading…"
            : usingSnapshot
              ? "as read during the audit"
              : `re-read from disk · ${rule?.journalRef ?? "—"}`}
        </span>
      </div>

      <div className="space-y-4 px-4 py-3">
        <div>
          <div className="label mb-1">the rule that drifted</div>
          <div className="mb-1.5 flex flex-wrap items-baseline gap-2">
            <span className="path">{finding.evidence.path}</span>
            <span className="text-[11px] text-brass">:{finding.evidence.line}</span>
          </div>
          {error && !usingSnapshot ? (
            <p className="rounded border border-crack/40 bg-crack/10 px-2.5 py-2 text-[11.5px] text-[#f0a79d]">
              could not read it back: {error}
            </p>
          ) : usingSnapshot ? (
            <>
              <Rows
                rows={snapshot}
                cite={finding.evidence.line}
                note="the source is no longer on disk (a cloned repo is deleted when the run ends), so this is the exact text the audit journaled"
              />
              {finding.evidence.journalRef && (
                <p className="mt-1 text-[10px] text-bench-500">
                  journaled as {finding.evidence.journalRef}
                </p>
              )}
            </>
          ) : (
            <CodeBlock payload={rule} cite={finding.evidence.line} />
          )}
        </div>

        <div>
          <div className="label mb-1">what the repo actually says</div>
          <p className="mb-1.5 text-[11.5px] leading-relaxed text-[#d8c39a]">
            {finding.reality}
          </p>
          {reality && (
            <div className="mb-1.5 flex flex-wrap items-baseline gap-2">
              <span className="path">{reality.path}</span>
              <span className="text-[11px] text-crack">:{reality.line}</span>
              {reality.label && (
                <span className="text-[10px] uppercase tracking-wider text-bench-500">
                  {reality.label}
                </span>
              )}
            </div>
          )}
          {reality && code ? (
            <CodeBlock payload={code} cite={reality.line} tone="crack" />
          ) : reality && !loading ? (
            <Rows
              rows={[{ line: reality.line, text: reality.text }]}
              cite={reality.line}
              tone="crack"
              note="quoted from the audit journal; this file is no longer on disk"
            />
          ) : reality ? (
            <div className="h-16 rounded border border-bench-800 bg-void/40" />
          ) : (
            <p className="text-[11px] text-bench-500">
              The contradicting reality is an absence (no matching file, script or binary), so
              there is no code line to point at — the proof is in the tool call above.
            </p>
          )}
        </div>

        <div className="border-t border-bench-800 pt-2.5 text-[10.5px] leading-relaxed text-bench-500">
          <div>
            proven by tool calls:{" "}
            <span className="text-rosin">
              {finding.evidence.journalRef || finding.journalRef}
            </span>
            {reality?.ref ? <span className="text-rosin"> · {reality.ref}</span> : null}
          </div>
          {finding.suggestion && (
            <div className="mt-1.5 text-[11.5px] text-varnish">
              <span className="text-cured">fix ·</span> {finding.suggestion}
            </div>
          )}
          {finding.evidence.unverifiedCitation && (
            <div className="mt-1.5 text-warp">
              model citation could not be verified, so this is reported as unknown:{" "}
              {finding.evidence.unverifiedCitation}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function Rows({
  rows,
  cite,
  tone = "brass",
  note,
}: {
  rows: { line: number; text: string }[];
  cite: number;
  tone?: "brass" | "crack";
  note?: string;
}) {
  const highlight =
    tone === "brass" ? "bg-brass/12 border-l-2 border-brass" : "bg-crack/12 border-l-2 border-crack";
  return (
    <div className="overflow-x-auto rounded border border-bench-800 bg-void/60">
      <table className="w-full border-collapse font-mono text-[11.5px] leading-[1.6]">
        <tbody>
          {rows.map((row) => (
            <tr key={row.line} className={row.line === cite ? highlight : ""}>
              <td className="w-[52px] select-none px-2 py-[1px] text-right text-bench-500 tabular-nums">
                {row.line}
              </td>
              <td className="whitespace-pre pr-3 text-varnish/90">
                {row.text === "" ? " " : row.text}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {note && (
        <div className="border-t border-bench-800 px-2.5 py-1 text-[10px] text-warp">{note}</div>
      )}
    </div>
  );
}

function CodeBlock({
  payload,
  cite,
  tone = "brass",
}: {
  payload: EvidencePayload | null;
  cite: number;
  tone?: "brass" | "crack";
}) {
  if (!payload) return <div className="h-16 rounded border border-bench-800 bg-void/40" />;
  if (!payload.exists) {
    return (
      <p className="rounded border border-crack/40 bg-crack/10 px-2.5 py-2 text-[11.5px] text-[#f0a79d]">
        this path is not on disk
      </p>
    );
  }
  return (
    <div>
      <Rows rows={payload.context} cite={payload.citedLine ?? cite} tone={tone} />
      {payload.truncated && (
        <div className="mt-1 text-[10px] text-warp">
          file was truncated at the byte cap — only the first lines were read
        </div>
      )}
    </div>
  );
}
