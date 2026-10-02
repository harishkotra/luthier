import { useMemo, useState } from "react";
import type { Category, Finding, Severity } from "../types";
import { SEVERITY_CLASS, clip } from "../format";

interface Props {
  findings: Finding[];
  selectedId: string | null;
  onSelect: (finding: Finding) => void;
  onShowDiff: (finding: Finding) => void;
}

const CATEGORY_TABS: { id: Category | "all"; label: string }[] = [
  { id: "all", label: "all" },
  { id: "paths", label: "paths" },
  { id: "commands", label: "commands" },
  { id: "skills", label: "skills" },
  { id: "mcp", label: "mcp" },
  { id: "conventions", label: "conventions" },
];

const SEVERITY_TABS: { id: Severity | "all"; label: string }[] = [
  { id: "all", label: "any severity" },
  { id: "critical", label: "critical" },
  { id: "high", label: "high" },
  { id: "medium", label: "medium" },
  { id: "low", label: "low" },
  { id: "info", label: "info" },
];

export default function Findings({ findings, selectedId, onSelect, onShowDiff }: Props) {
  const [category, setCategory] = useState<Category | "all">("all");
  const [severity, setSeverity] = useState<Severity | "all">("all");
  const [query, setQuery] = useState("");

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return findings
      .filter((finding) => (category === "all" ? true : finding.category === category))
      .filter((finding) => (severity === "all" ? true : finding.severity === severity))
      .filter((finding) =>
        needle
          ? [finding.title, finding.ruleText, finding.ruleFile, finding.reality]
              .join(" ")
              .toLowerCase()
              .includes(needle)
          : true,
      )
      .sort((a, b) => {
        const skipped = Number(a.skipped) - Number(b.skipped);
        if (skipped !== 0) return skipped;
        return a.id.localeCompare(b.id, undefined, { numeric: true });
      });
  }, [findings, category, severity, query]);

  return (
    <section className="panel">
      <div className="panel-head flex-wrap">
        <div className="flex items-center gap-2">
          <h2 className="text-[13px] text-varnish">Findings</h2>
          <span className="text-[11px] text-bench-500">
            {rows.length} of {findings.length}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {CATEGORY_TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setCategory(tab.id)}
              className={`chip ${
                category === tab.id
                  ? "border-brass/60 bg-brass/15 text-brass"
                  : "border-bench-700 text-bench-500 hover:text-rosin"
              }`}
            >
              {tab.label}
              {tab.id !== "all" && (
                <span className="tabular-nums opacity-70">
                  {findings.filter((f) => f.category === tab.id).length}
                </span>
              )}
            </button>
          ))}
          <select
            value={severity}
            onChange={(event) => setSeverity(event.target.value as Severity | "all")}
            className="rounded border border-bench-700 bg-void/70 px-1.5 py-[2px] text-[11px] text-rosin"
          >
            {SEVERITY_TABS.map((tab) => (
              <option key={tab.id} value={tab.id}>
                {tab.label}
              </option>
            ))}
          </select>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="filter…"
            className="w-32 rounded border border-bench-700 bg-void/70 px-2 py-[3px] text-[11px] text-varnish placeholder:text-bench-500 focus:border-brass/60"
          />
        </div>
      </div>

      <div className="max-h-[46vh] overflow-auto">
        <table className="w-full border-collapse text-left text-[12px]">
          <thead className="sticky top-0 z-10 bg-bench-900/95 text-[10px] uppercase tracking-[0.14em] text-bench-500 backdrop-blur">
            <tr>
              <th className="px-3 py-2 font-normal">sev</th>
              <th className="px-3 py-2 font-normal">category</th>
              <th className="px-3 py-2 font-normal">what drifted</th>
              <th className="px-3 py-2 font-normal">rule</th>
              <th className="px-3 py-2 font-normal">contradicting reality</th>
              <th className="px-3 py-2 font-normal">fix</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((finding) => {
              const active = finding.id === selectedId;
              return (
                <tr
                  key={finding.id}
                  onClick={() => onSelect(finding)}
                  className={`cursor-pointer border-t border-bench-800 align-top transition-colors ${
                    active ? "bg-brass/[0.07]" : "hover:bg-bench-800/60"
                  } ${finding.skipped ? "opacity-70" : ""}`}
                >
                  <td className="px-3 py-2.5">
                    <span className={`chip ${SEVERITY_CLASS[finding.severity]}`}>
                      {finding.severity}
                    </span>
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="text-rosin">{finding.category}</div>
                    <div className="text-[10px] text-bench-500">{finding.checkLabel}</div>
                    {finding.advisory && (
                      <div className="mt-1 text-[9.5px] uppercase tracking-wider text-spruce">
                        advisory · 0 pt
                      </div>
                    )}
                    {finding.skipped && (
                      <div className="mt-1 text-[9.5px] uppercase tracking-wider text-warp">
                        skipped · 0 pt
                      </div>
                    )}
                  </td>
                  <td className="max-w-[280px] px-3 py-2.5">
                    <div className="text-varnish">{finding.title}</div>
                    {finding.occurrences.length > 1 && (
                      <div className="mt-1 text-[10px] text-bench-500">
                        also at {finding.occurrences.slice(1, 4).join(", ")}
                      </div>
                    )}
                    {finding.folded > 0 && (
                      <div className="mt-1 text-[10px] text-warp/80">
                        +{finding.folded} similar folded in
                      </div>
                    )}
                  </td>
                  <td className="max-w-[240px] px-3 py-2.5">
                    <div className="path truncate" title={finding.ruleText}>
                      {clip(finding.ruleText, 72)}
                    </div>
                    <button
                      onClick={(event) => {
                        event.stopPropagation();
                        onSelect(finding);
                      }}
                      className="mt-1 text-[10.5px] text-brass underline decoration-dotted hover:text-varnish"
                    >
                      {finding.ruleFile}:{finding.ruleLine}
                    </button>
                  </td>
                  <td className="max-w-[300px] px-3 py-2.5 text-[11.5px] text-[#d8c39a]">
                    <span title={finding.reality}>{clip(finding.reality, 150)}</span>
                  </td>
                  <td className="px-3 py-2.5 whitespace-nowrap">
                    {finding.diff ? (
                      <button
                        onClick={(event) => {
                          event.stopPropagation();
                          onShowDiff(finding);
                        }}
                        className="btn px-2 py-1 text-[11px]"
                      >
                        show diff
                      </button>
                    ) : (
                      <span className="text-[10.5px] text-bench-500">
                        {finding.verdict === "unknown" && finding.skipped ? "unjudged" : "manual"}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center text-[12px] text-bench-500">
                  No findings match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
