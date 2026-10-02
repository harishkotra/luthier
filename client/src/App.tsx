import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getHealth, getPresets, getReport, startAudit, streamAudit } from "./api";
import {
  DEFAULT_SETTINGS,
  enabledCategories,
  loadSettings,
  saveSettings,
  type Settings,
} from "./settings";
import type { Finding, Health, Preset, ProgressEvent, Report, ScoringConstants } from "./types";
import { getScoring } from "./api";
import Scorecard from "./components/Scorecard";
import Findings from "./components/Findings";
import Evidence from "./components/Evidence";
import FixDiff from "./components/FixDiff";
import SettingsPanel from "./components/Settings";

type Phase = "empty" | "running" | "done" | "error";

const STAGES: { id: string; label: string; note?: string }[] = [
  { id: "fetch", label: "fetch source" },
  { id: "discover", label: "enumerate harness" },
  { id: "extract", label: "build rule inventory" },
  { id: "check-paths", label: "1 · path_exists", note: "deterministic" },
  { id: "check-commands", label: "2 · command_runs", note: "declared, never executed" },
  { id: "check-skills", label: "3 · file_referenced", note: "declared ⇄ used ⇄ listed" },
  { id: "check-mcp", label: "4 · server_reachable", note: "schema + binary, never spawned" },
  {
    id: "check-conventions",
    label: "5 · semantic",
    note: "mechanical subset offline, then LLM verdicts",
  },
  { id: "agent-loop", label: "strands verification" },
  { id: "scoring", label: "score drift" },
];

export default function App() {
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [constants, setConstants] = useState<ScoringConstants | null>(null);
  const [showSettings, setShowSettings] = useState(false);

  const [repoPath, setRepoPath] = useState("");
  const [pendingZip, setPendingZip] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("empty");
  const [events, setEvents] = useState<ProgressEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [selected, setSelected] = useState<Finding | null>(null);
  const closeStream = useRef<(() => void) | null>(null);

  useEffect(() => {
    const loaded = loadSettings();
    setSettings(loaded);
    // ?run=<id>[&finding=<id>] opens a finished audit (and one finding) straight from disk.
    const params = new URLSearchParams(window.location.search);
    const runParam = params.get("run");
    if (runParam) {
      setPhase("running");
      getReport(runParam)
        .then((result) => {
          setReport(result);
          // A cloned source is deleted after the run, so offer what the user actually typed.
          setRepoPath(
            result.source?.requested || result.source?.url || result.repo.path,
          );
          setPhase("done");
          const wanted = params.get("finding");
          if (wanted) {
            const hit = result.findings.find((finding) => finding.id === wanted);
            if (hit) setSelected(hit);
          }
        })
        .catch((err) => {
          setError(`run ${runParam}: ${String((err as Error).message ?? err)}`);
          setPhase("error");
        });
    }
    getPresets()
      .then((data) => {
        setPresets(data.presets);
        setRepoPath((current) => current || loaded.repoRoot || data.repoRootDefault || "");
      })
      .catch(() => setPresets([]));
    getHealth()
      .then((value) => {
        setHealth(value);
        setRepoPath((current) => current || loaded.repoRoot || value.repoRootDefault || "");
      })
      .catch(() => setHealth(null));
    getScoring().then(setConstants).catch(() => setConstants(null));
    return () => closeStream.current?.();
  }, []);

  useEffect(() => {
    if (settings === DEFAULT_SETTINGS) return;
    saveSettings(settings);
  }, [settings]);

  const stageState = useMemo(() => {
    const map = new Map<string, ProgressEvent>();
    for (const event of events) if (event.stage) map.set(event.stage, event);
    return map;
  }, [events]);

  const scan = useCallback(async () => {
    closeStream.current?.();
    setPhase("running");
    setEvents([]);
    setError(null);
    setReport(null);
    setSelected(null);
    try {
      const started = await startAudit({
        repoPath: repoPath.trim(),
        settings,
        categories: enabledCategories(settings),
        file: pendingZip,
      });
      const finish = async () => {
        try {
          const result = await getReport(started.id);
          setReport(result);
          setPhase("done");
          window.history.replaceState(null, "", `?run=${started.id}`);
        } catch (err) {
          setError(String((err as Error).message ?? err));
          setPhase("error");
        }
      };
      closeStream.current = streamAudit(
        started.id,
        (event) => {
          setEvents((current) => [...current, event]);
          if (event.type === "done") void finish();
        },
        (message) => {
          setError(message);
          setPhase("error");
        },
      );
    } catch (err) {
      setError(String((err as Error).message ?? err));
      setPhase("error");
    }
  }, [pendingZip, repoPath, settings]);

  const onDrop = (event: React.DragEvent) => {
    event.preventDefault();
    const file = event.dataTransfer.files?.[0];
    if (file && file.name.endsWith(".zip")) setPendingZip(file);
  };

  const clean = phase === "done" && report?.score === 100;
  const categoriesOff = enabledCategories(settings).length < 5;
  const isUrl = /^\s*(https?:\/\/|git@)/.test(repoPath);

  return (
    <div className="grain min-h-full">
      <header className="sticky top-0 z-20 border-b border-bench-700 bg-void/85 backdrop-blur">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-3 px-5 py-3">
          <div className="flex items-baseline gap-2.5">
            <h1 className="font-display text-[22px] leading-none text-varnish">Luthier</h1>
            <span className="text-[11px] text-bench-500">harness drift auditor</span>
          </div>

          <div className="ml-auto flex min-w-[380px] flex-1 flex-wrap items-center gap-2">
            <div className="relative flex-1">
              <input
                value={repoPath}
                onChange={(event) => setRepoPath(event.target.value)}
                onKeyDown={(event) => event.key === "Enter" && phase !== "running" && scan()}
                onDrop={onDrop}
                onDragOver={(event) => event.preventDefault()}
                placeholder="/absolute/path/to/repo · a repo URL · or drop a .zip"
                disabled={Boolean(pendingZip) || phase === "running"}
                className="field h-[34px]"
              />
              {pendingZip && (
                <button
                  onClick={() => setPendingZip(null)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-[11px] text-warp hover:text-varnish"
                >
                  {pendingZip.name} ✕
                </button>
              )}
            </div>
            <button onClick={scan} disabled={phase === "running"} className="btn btn-primary h-[34px]">
              {phase === "running" ? "auditing…" : "audit harness"}
            </button>
            <button onClick={() => setShowSettings(true)} className="btn h-[34px]">
              settings
            </button>
          </div>

          <div className="flex w-full items-center gap-3 text-[10.5px] text-bench-500">
            {isUrl && !pendingZip && (
              <span className="text-brass">
                shallow clone → .luthier_sources/&lt;run&gt; · .git removed · deleted when the run
                ends
              </span>
            )}
            <span className={health ? "text-cured" : "text-crack"}>
              {health ? "agent :3001 up" : "agent :3001 unreachable"}
            </span>
            <span>
              {settings.apiKey || health?.inferenceConfigured
                ? `semantic checks on · ${health?.model ?? settings.model}`
                : "deterministic only · no API key"}
            </span>
            {categoriesOff && (
              <span className="text-warp">
                {5 - enabledCategories(settings).length} category toggle(s) off
              </span>
            )}
            {health?.auditLog && (
              <span className="ml-auto truncate" title={health.auditLog}>
                journal: {health.auditLog}
              </span>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1400px] px-5 py-5">
        {phase === "empty" && <EmptyState onPickZip={setPendingZip} />}

        {phase === "running" && (
          <Scanning stages={STAGES} state={stageState} events={events} />
        )}

        {phase === "error" && (
          <section className="panel border-crack/40">
            <div className="panel-head">
              <h2 className="text-[13px] text-[#f0a79d]">Audit failed</h2>
            </div>
            <p className="px-4 py-4 text-[12px] leading-relaxed text-[#d8c39a]">{error}</p>
            <p className="px-4 pb-4 text-[11px] text-bench-500">
              Check the repo path exists and is readable, and that the agent is running on :3001.
            </p>
          </section>
        )}

        {phase === "done" && report && (
          <div className="space-y-5">
            <Scorecard report={report} constants={constants} />

            {clean && (
              <section className="panel border-cured/40 bg-cured/[0.06]">
                <div className="flex items-center gap-3 px-5 py-4">
                  <span className="font-display text-[15px] text-[#9ed7b0]">No drift found.</span>
                  <span className="text-[11.5px] text-bench-500">
                    Every rule in {report.rules.length} inventoried claim matched the repo —
                    {report.repo.harnessFiles.length} instruction file(s),{" "}
                    {report.repo.mcpConfigs.length} MCP config(s),{" "}
                    {report.repo.skillDirs.length} skill dir(s) checked.
                  </span>
                </div>
              </section>
            )}

            <div className="grid gap-5 xl:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
              <div className="space-y-5">
                <Findings
                  findings={report.findings}
                  selectedId={selected?.id ?? null}
                  onSelect={setSelected}
                  onShowDiff={setSelected}
                />
                <Extraction report={report} />
              </div>
              <div className="space-y-5">
                <Evidence finding={selected} repo={report.repo.path} />
                <FixDiff finding={selected} />
                <Narrative report={report} />
              </div>
            </div>
          </div>
        )}
      </main>

      <footer className="mx-auto max-w-[1400px] px-5 pb-8 pt-2 text-[11px] text-bench-500">
        <p>
          Read-only by design: Luthier never writes to the audited repo, never executes a
          documented command, and never spawns an MCP server.
        </p>
        <p className="mt-1.5">
          Built by{" "}
          <a
            href="https://harishkotra.me"
            target="_blank"
            rel="noreferrer"
            className="text-brass underline decoration-dotted hover:text-varnish"
          >
            Harish Kotra
          </a>{" "}
          ·{" "}
          <a
            href="https://dailybuild.xyz"
            target="_blank"
            rel="noreferrer"
            className="text-brass underline decoration-dotted hover:text-varnish"
          >
            Checkout my other builds
          </a>
        </p>
      </footer>

      {showSettings && (
        <SettingsPanel
          settings={settings}
          presets={presets}
          onChange={setSettings}
          onClose={() => setShowSettings(false)}
        />
      )}
    </div>
  );
}

function EmptyState({ onPickZip }: { onPickZip: (file: File) => void }) {
  return (
    <section className="panel">
      <div className="px-8 py-10">
        <h2 className="font-display text-[26px] text-varnish">
          Your agent's harness is a set of claims about this repo.
        </h2>
        <p className="mt-2 max-w-[640px] text-[13px] leading-relaxed text-[#d8c39a]">
          Luthier extracts every one of those claims, checks it against what is actually on disk,
          and reports the drift — stale paths, commands nothing declares, dead MCP servers, skills
          no file references, and conventions the code broke. Each finding cites{" "}
          <span className="path">file:line</span> and carries a patch you can apply.
        </p>
        <div className="mt-6 grid gap-2 text-[12px] sm:grid-cols-2 lg:grid-cols-5">
          {STAGES.filter((stage) => stage.id.startsWith("check")).map((stage) => (
            <div key={stage.id} className="rounded border border-bench-800 bg-void/40 px-3 py-2.5">
              <div className="text-rosin">{stage.label}</div>
              <div className="mt-1 text-[10.5px] text-bench-500">{stage.note}</div>
            </div>
          ))}
        </div>
        <label className="btn mt-6 cursor-pointer">
          drop a .zip instead
          <input
            type="file"
            accept=".zip"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) onPickZip(file);
            }}
          />
        </label>
        <p className="mt-3 text-[11px] text-bench-500">
          Or type a repo path / GitHub URL and press Enter. A URL is shallow-cloned into
          <span className="path"> .luthier_sources/</span>, audited, then deleted — nothing leaves
          this machine unless you configure an inference endpoint.
        </p>
      </div>
    </section>
  );
}

function Scanning({
  stages,
  state,
  events,
}: {
  stages: { id: string; label: string }[];
  state: Map<string, ProgressEvent>;
  events: ProgressEvent[];
}) {
  const last = [...events].reverse().find((event) => event.detail);
  if (events.length === 0) {
    return (
      <section className="panel">
        <div className="panel-head">
          <h2 className="text-[13px] text-varnish">Loading run</h2>
        </div>
        <p className="px-4 py-6 text-center text-[12px] text-bench-500">
          reading the stored report from disk…
        </p>
      </section>
    );
  }
  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="text-[13px] text-varnish">Auditing</h2>
        <span className="text-[10.5px] text-bench-500">live, one line per check</span>
      </div>
      <ol className="divide-y divide-bench-800">
        {stages.map((stage) => {
          const event = state.get(stage.id);
          const status = event?.status ?? "waiting";
          const tone =
            status === "done"
              ? "text-[#9ed7b0]"
              : status === "running"
                ? "text-brass"
                : status === "skipped" || status === "warning"
                  ? "text-warp"
                  : "text-bench-500";
          return (
            <li key={stage.id} className="flex items-start gap-3 px-4 py-2.5 text-[12px]">
              <span className={`mt-[3px] w-2 shrink-0 ${
                  status === "running"
                    ? "animate-pulse rounded-full bg-brass h-2"
                    : status === "done"
                      ? "rounded-full bg-cured h-2"
                      : "rounded-full bg-bench-600 h-2"
                }`} />
              <span className="w-[190px] shrink-0 text-varnish">{stage.label}</span>
              <span className={`w-[68px] shrink-0 uppercase tracking-wider text-[10px] ${tone}`}>
                {status}
              </span>
              <span className="min-w-0 flex-1 truncate text-[11px] text-bench-500">
                {event?.detail ?? ""}
              </span>
            </li>
          );
        })}
      </ol>
      {last?.detail && (
        <div className="border-t border-bench-800 px-4 py-2.5 text-[11px] text-rosin">
          {last.stage} · {last.detail}
        </div>
      )}
    </section>
  );
}

function Extraction({ report }: { report: Report }) {
  const extraction = report.extraction;
  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="text-[13px] text-varnish">
          Rule inventory <span className="text-[10.5px] text-bench-500">{extraction.total}</span>
        </h2>
        <span className="text-[10.5px] text-bench-500">
          static {extraction.static} · model {extraction.llmAccepted} · rejected{" "}
          {extraction.rejectedCount}
        </span>
      </div>
      <div className="max-h-[220px] overflow-auto px-4 py-3 text-[11.5px]">
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-bench-500">
          {Object.entries(extraction.byKind ?? {}).map(([kind, count]) => (
            <span key={kind}>
              {kind} <span className="text-rosin">{count}</span>
            </span>
          ))}
        </div>
        <ul className="mt-2 space-y-1">
          {report.rules.slice(0, 60).map((rule) => (
            <li key={rule.id} className="flex gap-2">
              <span className="w-[76px] shrink-0 text-[10px] uppercase tracking-wider text-bench-500">
                {rule.checkLabel}
              </span>
              <span className="min-w-0 flex-1 truncate text-varnish" title={rule.text}>
                {rule.text}
              </span>
              <span className="path shrink-0 text-rosin/80">
                {rule.source}:{rule.line}
              </span>
            </li>
          ))}
        </ul>
        {extraction.rejectedCount > 0 && (
          <p className="mt-3 border-t border-bench-800 pt-2 text-[10.5px] text-warp">
            {extraction.rejectedCount} model-supplied rule(s) rejected because the quote was not
            found at the cited line — the model cannot invent a rule.
          </p>
        )}
      </div>
    </section>
  );
}

function Narrative({ report }: { report: Report }) {
  const narrative = report.agentNarrative;
  const calls = report.toolCalls;
  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="text-[13px] text-varnish">Agent account</h2>
        <span className="text-[10.5px] text-bench-500">
          {narrative?.status === "ok"
            ? `strands · ${narrative.toolCalls} tool call(s)`
            : (narrative?.status ?? "not run")}
        </span>
      </div>
      <div className="px-4 py-3 text-[12px] leading-relaxed">
        {narrative?.text ? (
          <p className="text-varnish">{narrative.text}</p>
        ) : (
          <p className="text-bench-500">
            The Strands loop did not run ({narrative?.status ?? "no endpoint configured"}). The
            scorecard above is entirely deterministic, so it is still trustworthy — the conventions
            the model would have judged are listed as skipped.
          </p>
        )}
        <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 border-t border-bench-800 pt-2.5 text-[10.5px] text-bench-500 sm:grid-cols-3">
          {Object.entries(calls.byTool ?? {}).map(([tool, stats]) => (
            <span key={tool}>
              <span className="path text-rosin">{tool}</span> {stats.calls}× ·{" "}
              {stats.bytes.toLocaleString()} B
              {stats.errors ? ` · ${stats.errors} err` : ""}
            </span>
          ))}
        </div>
        <p className="mt-2 text-[10px] text-bench-500">
          {calls.total} journaled tool calls, {calls.bytesReturned.toLocaleString()} bytes read.
          Every claim on this page traces back to one of them.
        </p>
      </div>
    </section>
  );
}
