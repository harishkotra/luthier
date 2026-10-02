import { useState } from "react";
import { testConnection } from "../api";
import { ALL_CATEGORIES, CATEGORY_HINTS, type Settings } from "../settings";
import type { ConnectionResult, Preset } from "../types";

interface Props {
  settings: Settings;
  presets: Preset[];
  onChange: (next: Settings) => void;
  onClose: () => void;
}

export default function SettingsPanel({ settings, presets, onChange, onClose }: Props) {
  const [result, setResult] = useState<ConnectionResult | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);

  const patch = (part: Partial<Settings>) => onChange({ ...settings, ...part });

  const applyPreset = (preset: Preset) =>
    patch({
      presetId: preset.id,
      baseUrl: preset.baseUrl,
      model: preset.model,
    });

  const probe = async () => {
    setProbing(true);
    setProbeError(null);
    try {
      setResult(await testConnection(settings));
    } catch (error) {
      setResult(null);
      setProbeError(String((error as Error).message ?? error));
    } finally {
      setProbing(false);
    }
  };

  return (
    <div className="fixed inset-0 z-30 flex items-start justify-center overflow-auto bg-void/80 p-6 backdrop-blur-sm">
      <div className="panel w-full max-w-[840px]">
        <div className="panel-head">
          <h2 className="text-[13px] text-varnish">Settings</h2>
          <div className="flex items-center gap-3">
            <span className="text-[10px] text-bench-500">
              stored locally · key never echoed by the server
            </span>
            <button onClick={onClose} className="btn px-2 py-0.5 text-[11px]">
              close
            </button>
          </div>
        </div>

        <div className="grid gap-6 p-5 md:grid-cols-2">
          <div className="space-y-4">
            <div>
              <div className="label mb-1.5">Inference preset</div>
              <div className="flex flex-wrap gap-1.5">
                {presets.map((preset) => (
                  <button
                    key={preset.id}
                    onClick={() => applyPreset(preset)}
                    className={`chip ${
                      settings.presetId === preset.id
                        ? "border-brass/60 bg-brass/15 text-brass"
                        : "border-bench-700 text-bench-500 hover:text-rosin"
                    }`}
                  >
                    {preset.label}
                    {preset.default && <span className="opacity-60">default</span>}
                  </button>
                ))}
              </div>
            </div>

            <Field label="Base URL">
              <input
                className="field"
                value={settings.baseUrl}
                placeholder="https://api.particle.ai/v1"
                onChange={(event) => patch({ baseUrl: event.target.value })}
              />
            </Field>

            <Field label="Model">
              <input
                className="field"
                value={settings.model}
                placeholder="deepseek-v4.1-flash"
                onChange={(event) => patch({ model: event.target.value })}
              />
            </Field>

            <Field label="API key">
              <input
                className="field"
                type="password"
                value={settings.apiKey}
                placeholder={
                  settings.baseUrl.includes("localhost")
                    ? "not required for a local server"
                    : "pa-…"
                }
                onChange={(event) => patch({ apiKey: event.target.value })}
              />
              <p className="mt-1 text-[10px] text-bench-500">
                Sent only with the request that needs it. The server responds with a redacted
                preview, never the key itself.
              </p>
            </Field>

            <div className="space-y-2">
              <button onClick={probe} disabled={probing} className="btn btn-primary">
                {probing ? "testing…" : "Test connection"}
              </button>
              {probeError && (
                <p className="rounded border border-crack/40 bg-crack/10 px-2.5 py-2 text-[11px] text-[#f0a79d]">
                  {probeError}
                </p>
              )}
              {result && (
                <div className="rounded border border-bench-700 bg-void/60 p-2.5 text-[11px]">
                  <div className="flex items-center gap-2">
                    <span
                      className={`chip ${
                        result.ok
                          ? "border-cured/50 bg-cured/15 text-[#9ed7b0]"
                          : "border-crack/50 bg-crack/10 text-[#f0a79d]"
                      }`}
                    >
                      {result.ok ? "reachable" : "failed"}
                    </span>
                    <span className="text-bench-500">
                      {result.baseUrl} · {result.model}
                    </span>
                    {result.error && <span className="text-[#f0a79d]">{result.error}</span>}
                  </div>
                  <ul className="mt-2 space-y-1.5">
                    {result.steps.map((step, index) => (
                      <li key={index} className="border-t border-bench-800 pt-1.5">
                        <div className="flex gap-2">
                          <span className="text-rosin">
                            {step.method} {step.status ?? "—"}
                          </span>
                          <span className="text-bench-500">
                            {step.latencyMs !== null ? `${step.latencyMs}ms` : ""}
                          </span>
                          {step.error && <span className="text-[#f0a79d]">{step.error}</span>}
                        </div>
                        <div className="path mt-0.5 break-all opacity-70">{step.url}</div>
                        <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-all text-[10px] text-bench-500">
                          {step.body.slice(0, 700)}
                        </pre>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </div>

          <div className="space-y-4">
            <div>
              <div className="label mb-1.5">Audit scope</div>
              <Field label="Default repo path">
                <input
                  className="field"
                  value={settings.repoRoot}
                  placeholder="/absolute/path/to/repo"
                  onChange={(event) => patch({ repoRoot: event.target.value })}
                />
              </Field>
              <p className="mt-1 text-[10px] text-bench-500">
                Seeds the path box on the main screen. The repo itself is only ever read.
              </p>
            </div>

            <div>
              <div className="label mb-1.5">Check categories</div>
              <div className="space-y-1.5">
                {ALL_CATEGORIES.map((category) => (
                  <label
                    key={category}
                    className="flex cursor-pointer items-start gap-2.5 rounded border border-bench-800 bg-void/40 px-2.5 py-2 hover:border-bench-600"
                  >
                    <input
                      type="checkbox"
                      className="mt-1 accent-[#c8963e]"
                      checked={settings.categories[category]}
                      onChange={(event) =>
                        patch({
                          categories: {
                            ...settings.categories,
                            [category]: event.target.checked,
                          },
                        })
                      }
                    />
                    <span>
                      <span className="text-[12px] text-varnish">{category}</span>
                      <span className="block text-[10.5px] text-bench-500">
                        {CATEGORY_HINTS[category]}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
              <p className="mt-2 text-[10.5px] leading-relaxed text-bench-500">
                Disabled categories report <span className="path">skipped: category disabled</span>{" "}
                and contribute nothing to the score, so a 100 always means "no drift found in the
                categories you asked about".
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="label mb-1 block">{label}</span>
      {children}
    </label>
  );
}
