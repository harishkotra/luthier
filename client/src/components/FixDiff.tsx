import { useState } from "react";
import type { Finding } from "../types";

interface Props {
  finding: Finding | null;
}

export default function FixDiff({ finding }: Props) {
  const [copied, setCopied] = useState(false);
  const diff = finding?.diff ?? "";

  const copy = async () => {
    if (!diff) return;
    try {
      await navigator.clipboard.writeText(diff);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked (insecure origin) -- the text below is selectable */
    }
  };

  if (!finding) {
    return (
      <section className="panel">
        <div className="panel-head">
          <h2 className="text-[13px] text-varnish">Fix diff</h2>
        </div>
        <p className="px-4 py-8 text-center text-[12px] text-bench-500">
          Pick a finding with a diff to see a patch you can apply.
        </p>
      </section>
    );
  }

  if (!diff) {
    return (
      <section className="panel">
        <div className="panel-head">
          <h2 className="text-[13px] text-varnish">Fix diff</h2>
        </div>
        <div className="px-4 py-4 text-[12px] leading-relaxed text-bench-500">
          <p className="text-varnish">No automatic patch for this one.</p>
          <p className="mt-2">
            {finding.check === "convention_llm"
              ? "A convention violation needs a human call: fix the code, or amend the rule to match what the repo actually does."
              : "Luthier only emits a diff when it can name the exact replacement. Anything looser would be a guess, so it is written as a suggestion instead."}
          </p>
          {finding.suggestion && (
            <p className="mt-3 rounded border border-spruce/40 bg-spruce/10 px-2.5 py-2 text-[11.5px] text-[#a9d6c5]">
              {finding.suggestion}
            </p>
          )}
        </div>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="text-[13px] text-varnish">
          Fix diff <span className="ml-1 text-[10.5px] text-bench-500">{finding.ruleFile}</span>
        </h2>
        <button onClick={copy} className="btn px-2 py-1 text-[11px]">
          {copied ? "copied" : "copy patch"}
        </button>
      </div>

      <pre className="max-h-[38vh] overflow-auto px-3 py-2.5 font-mono text-[11.5px] leading-[1.55]">
        {diff.split("\n").map((line, index) => (
          <div
            key={index}
            className={
              line.startsWith("+++") || line.startsWith("---")
                ? "text-brass"
                : line.startsWith("@@")
                  ? "text-[#7fc2ab]"
                  : line.startsWith("+")
                    ? "bg-cured/10 text-[#b6dcc0]"
                    : line.startsWith("-")
                      ? "bg-crack/10 text-[#eda79e]"
                      : "text-bench-500"
            }
          >
            {line === "" ? " " : line}
          </div>
        ))}
      </pre>

      <div className="border-t border-bench-800 px-4 py-2.5 text-[10.5px] leading-relaxed">
        <p className="text-warp">
          Luthier never writes to the audited repo. Nothing here has been applied — review it,
          then apply it yourself:
        </p>
        <p className="mt-1 path select-all text-rosin">
          git apply review-this-patch-before-running-it.patch
        </p>
        <p className="mt-1 text-bench-500">
          Validated against <span className="path">{finding.ruleFile}</span> as it sits on disk;
          re-run the audit after applying to confirm the finding clears.
        </p>
      </div>
    </section>
  );
}
