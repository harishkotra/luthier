import type { Category, Report, ScoringConstants } from "../types";
import { formatTime, scoreTone } from "../format";

interface Props {
  report: Report;
  constants: ScoringConstants | null;
}

export default function Scorecard({ report, constants }: Props) {
  const table = constants ?? report.constants;
  const scoring = report.scoring;
  const score = report.score;
  const tone = scoreTone(score);
  const categories = (Object.keys(scoring.perCategory) as Category[]).filter(
    (key) => scoring.perCategory[key],
  );

  return (
    <section className="panel overflow-hidden">
      <div className="flex flex-col gap-5 p-5 sm:flex-row sm:items-stretch">
        {/* The number, marked like a tuning scale. */}
        <div className="flex min-w-[190px] flex-col justify-between">
          <div className="label">drift score</div>
          <div className="flex items-end gap-2">
            <span
              className={`font-display text-[76px] leading-[0.85] tabular-nums ${tone.text}`}
            >
              {score}
            </span>
            <span className="pb-2 text-[13px] text-bench-500">/ {table.baseScore}</span>
          </div>
          <div className="mt-1 flex items-center gap-2">
            <span className={`chip border-current/40 ${tone.text}`}>{tone.word}</span>
            {scoring.deterministicFailures > 0 && (
              <span className="text-[11.5px] text-bench-500">
                {scoring.deterministicFailures} deterministic failure(s)
              </span>
            )}
          </div>
          <div className="mt-4 h-[3px] w-full rounded bg-bench-700">
            <div
              className="h-full rounded bg-gradient-to-r from-crack via-warp to-cured"
              style={{ width: `${Math.max(1.5, score)}%` }}
            />
          </div>
          <div className="mt-1 flex justify-between text-[9.5px] tracking-wider text-bench-500">
            <span>0 · unplayable</span>
            <span>{table.baseScore} · in tune</span>
          </div>
        </div>

        <div className="flex-1">
          <div className="grid grid-cols-1 gap-x-6 gap-y-1.5 text-[12px] sm:grid-cols-2">
            <Fact
              label={report.source?.kind === "url" ? "cloned into" : "repo"}
              value={report.repo.path}
              mono
            />
            {report.source?.kind === "url" && (
              <Fact
                label="source"
                value={`${report.source.url}${
                  report.source.commit ? ` @ ${report.source.commit}` : ""
                }`}
                mono
              />
            )}
            {report.source?.kind === "zip" && (
              <Fact label="source" value={report.source.file ?? "uploaded archive"} mono />
            )}
            <Fact label="run" value={report.runId} mono />
            <Fact label="scanned" value={formatTime(report.startedAt)} />
            <Fact
              label="duration"
              value={
                report.durationSeconds !== undefined
                  ? `${report.durationSeconds.toFixed(2)}s`
                  : "—"
              }
            />
            <Fact label="files read" value={String(report.repo.files)} />
            <Fact label="rules inventoried" value={String(report.rules.length)} />
            <Fact
              label="inference"
              value={
                report.inference?.used
                  ? `${report.inference.model} @ ${report.inference.baseUrl}`
                  : "deterministic only"
              }
            />
            <Fact
              label="tool calls"
              value={`${report.toolCalls.total} · ${report.toolCalls.bytesReturned.toLocaleString()} B`}
            />
          </div>

          <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-5">
            {categories.map((category) => {
              const ledger = scoring.perCategory[category];
              return (
                <div
                  key={category}
                  className="rounded border border-bench-700 bg-void/50 px-2.5 py-2"
                >
                  <div className="flex items-baseline justify-between">
                    <span className="text-[11px] text-rosin">{ledger.label}</span>
                    <span className="font-display text-[20px] tabular-nums text-varnish">
                      {ledger.count}
                    </span>
                  </div>
                  <div className="mt-0.5 text-[10px] text-bench-500">
                    −{ledger.appliedPenalty} pt
                    {ledger.capped ? " (capped)" : ""}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <details className="border-t border-bench-700 bg-void/40 px-5 py-3">
        <summary className="cursor-pointer select-none text-[11.5px] text-rosin">
          how this score was computed
        </summary>
        <div className="mt-3 grid gap-4 sm:grid-cols-2">
          <table className="w-full text-[11.5px]">
            <tbody>
              <ScoreRow
                label="deterministic failure"
                value={`−${table.penalties.deterministicFailure} each`}
                note="path, command, skill, MCP"
              />
              <ScoreRow
                label="semantic violation"
                value={`−${table.penalties.semanticViolation} each`}
                note="convention the code breaks"
              />
              <ScoreRow
                label="unknown verdict"
                value={`−${table.penalties.semanticUnknown} each`}
                note="nobody could confirm it"
              />
              <ScoreRow
                label="advisory / skipped"
                value={`−${table.advisoryPenalty}`}
                note="shown, never scored"
              />
              <ScoreRow
                label="per-category cap"
                value={`−${table.maxPenaltyPerCategory} max`}
                note="one area cannot sink the whole score"
              />
            </tbody>
          </table>
          <div className="text-[11px] leading-relaxed text-bench-500">
            <p className="path break-words">{table.formula}</p>
            <p className="mt-2">
              Raw penalty −{scoring.totalRawPenalty}, applied −
              {scoring.totalAppliedPenalty} after caps. Findings per check are folded at{" "}
              {table.maxFindingsPerCheck}.
            </p>
            {report.skipped.length > 0 && (
              <ul className="mt-2 space-y-0.5">
                {report.skipped.map((skip, index) => (
                  <li key={index} className="text-warp/90">
                    {skip.stage}: {skip.reason}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </details>
    </section>
  );
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex min-w-0 flex-col">
      <span className="label">{label}</span>
      <span className={`truncate text-[12px] text-varnish ${mono ? "font-mono" : ""}`} title={value}>
        {value}
      </span>
    </div>
  );
}

function ScoreRow({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  note: string;
}) {
  return (
    <tr>
      <td className="py-[3px] pr-2 text-rosin">{label}</td>
      <td className="py-[3px] pr-2 tabular-nums text-varnish">{value}</td>
      <td className="py-[3px] text-bench-500">{note}</td>
    </tr>
  );
}
