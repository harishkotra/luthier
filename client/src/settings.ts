import type { Category } from "./types";

export const SETTINGS_KEY = "luthier.settings.v1";

export type CategoryToggles = Record<Category, boolean>;

export interface Settings {
  version: 1;
  presetId: string;
  baseUrl: string;
  apiKey: string;
  model: string;
  repoRoot: string;
  categories: CategoryToggles;
}

export const ALL_CATEGORIES: Category[] = ["paths", "commands", "skills", "mcp", "conventions"];

export const CATEGORY_HINTS: Record<Category, string> = {
  paths: "path_exists — every path a rule names must resolve",
  commands: "command_runs — every documented command must be declared",
  skills: "file_referenced — declared skills used, on-disk skills listed",
  mcp: "server_reachable — MCP schema valid, binary resolvable",
  conventions: "semantic — conventions the code actually follows",
};

export const DEFAULT_SETTINGS: Settings = {
  version: 1,
  presetId: "particle",
  baseUrl: "https://api.particle.ai/v1",
  apiKey: "",
  model: "deepseek-v4.1-flash",
  repoRoot: "",
  categories: {
    paths: true,
    commands: true,
    skills: true,
    mcp: true,
    conventions: true,
  },
};

export function loadSettings(): Settings {
  if (typeof localStorage === "undefined") return DEFAULT_SETTINGS;
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return DEFAULT_SETTINGS;
    const parsed = JSON.parse(raw) as Partial<Settings>;
    return {
      ...DEFAULT_SETTINGS,
      ...parsed,
      categories: { ...DEFAULT_SETTINGS.categories, ...(parsed.categories ?? {}) },
      version: 1,
    };
  } catch {
    return DEFAULT_SETTINGS;
  }
}

export function saveSettings(settings: Settings): void {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    /* private mode or quota -- the audit still runs with defaults */
  }
}

export function enabledCategories(settings: Settings): Category[] {
  return ALL_CATEGORIES.filter((category) => settings.categories[category]);
}
