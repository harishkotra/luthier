"""Rule inventory and the five drift checks.

Checks 1-4 (`path_exists`, `command_runs`, `file_referenced`, `server_reachable`) are
deterministic: no LLM, no network, no execution. Check 5 (`semantic`) has a mechanical
subset that also runs offline, plus an LLM-judged pass that reports
`skipped: no_api_key` when no key is configured.

Every fact this module asserts comes from a journaled `read_harness` / `inspect_repo`
call, so any finding can be traced back to a real byte on disk.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .inference import ChatConfig, chat_json
from .scoring import CHECK_LABELS, MAX_FINDINGS_PER_CHECK, compute_score, scoring_constants
from .tools import Hit, ToolBundle, parse_numbered

ProgressFn = Callable[[dict[str, Any]], Any] | None

# ---------------------------------------------------------------- discovery

INSTRUCTION_GLOBS = [
    "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENT.md", "GEMINI.md", "QODER.md",
    ".cursorrules", ".cursor/rules/**/*.mdc", ".windsurfrules",
    ".github/copilot-instructions.md", ".agents/instructions/**/*.md",
    "README.md", "README.rst", "CONTRIBUTING.md", "docs/**/*.md",
]
SKILL_GLOBS = [
    ".hermes/skills/**/*.md", ".claude/skills/**/*.md", ".cursor/skills/**/*.md",
    "skills/**/*.md", ".agents/skills/**/*.md", "**/SKILL.md",
]
MCP_GLOBS = [
    ".mcp.json", "mcp.json", ".mcp/*.json", "claude_desktop_config.json",
    ".claude/claude_desktop_config.json", ".cursor/mcp.json", ".vscode/mcp.json",
    ".hermes/mcp.json", "opencode.json", ".gemini/settings.json",
]
MANIFEST_GLOBS = [
    "package.json", "*/package.json", "packages/*/package.json", "Makefile", "makefile",
    "GNUmakefile", "justfile", "Justfile", "pyproject.toml", "setup.py", "requirements*.txt",
    "Pipfile", "Cargo.toml", "go.mod", "Gemfile", "Taskfile.yml",
]

MCP_SERVER_KEYS = ("mcpServers", "servers", "mcp")

KNOWN_EXTS = {
    "md", "mdx", "txt", "rst", "json", "jsonc", "yaml", "yml", "toml", "ini", "cfg", "conf",
    "ts", "tsx", "js", "jsx", "mjs", "cjs", "py", "pyi", "go", "rs", "rb", "php", "java", "kt",
    "swift", "c", "h", "cc", "cpp", "hpp", "sh", "bash", "zsh", "ps1", "sql", "graphql", "tf",
    "css", "scss", "html", "vue", "svelte", "lock", "example", "gradle", "xml", "env",
}
COMMON_TOP_DIRS = {
    "src", "lib", "libs", "app", "apps", "api", "cmd", "internal", "pkg", "tests", "test",
    "spec", "e2e", "docs", "doc", "scripts", "bin", "sbin", "tools", "client", "server",
    "frontend", "backend", "web", "ui", "public", "static", "assets", "components", "pages",
    "routes", "hooks", "store", "packages", "modules", "plugins", "examples", "samples",
    "fixtures", "migrations", "db", "config", "configs", "deploy", "infra", "templates",
    "prompts", "skills", ".github", ".claude", ".hermes", ".cursor",
}
PLACEHOLDER_DIRS = {"path", "to", "some", "your", "my", "example", "foo", "bar", "baz", "qux",
                    "etc", "domain", "n", "x"}
URL_TLDS = {"com", "org", "net", "io", "dev", "app", "sh", "me", "ai", "xyz", "co", "edu", "gov"}
SOURCE_EXTS = {"ts", "tsx", "js", "jsx", "mjs", "cjs", "py", "go", "rs", "rb", "java", "php",
               "c", "h", "cc", "cpp", "vue", "svelte", "swift", "kt"}
DOC_EXTS = {"md", "mdx", "rst", "txt"}

BASH_LANGS = {"", "bash", "sh", "shell", "zsh", "console", "shell-session", "terminal",
              "sh-session", "text", "plain", "fish", "powershell", "ps1"}
RUNNER_NAMES = {"npm", "pnpm", "yarn", "bun", "bunx", "npx", "deno", "make", "gmake", "just",
                "cargo", "go", "docker", "docker-compose", "uv", "poetry", "pip", "pip3",
                "python", "python3", "pytest", "ruff", "mypy", "black", "eslint", "prettier",
                "biome", "turbo", "gradle", "mvn", "node", "ruby", "php", "task", "terraform",
                "aws", "brew", "git", "tsx", "ts-node", "vitest", "jest"}
# Runner subcommands that need no declaration in package.json. Anything else after
# `npm`/`pnpm`/`yarn`/`bun` is a *script name* and must be declared.
NO_DECL_SUBCOMMANDS = {
    "install", "i", "add", "remove", "rm", "uninstall", "update", "upgrade", "outdated",
    "list", "ls", "link", "unlink", "exec", "dlx", "why", "doctor", "audit", "fund", "prune",
    "pack", "global", "init", "create", "config", "login", "logout", "whoami", "version",
    "view", "diff", "approve-builds", "approve-scripts", "rebuild", "store", "fetch", "ci",
    "dedupe", "workspaces", "workspace", "info", "search", "publish", "star", "unstar",
    "token", "profile", "org", "access", "deprecate", "dist-tags", "owner", "team",
    "unpublish", "completion", "help", "ping", "find", "root", "bin", "docs", "bugs",
    "staff", "ll", "unlink-package", "install-ci", "install-test", "link-to-build",
}
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
FENCE_RE = re.compile(r"^\s*(?:```|~~~)\s*([A-Za-z0-9_+#.\-]*)\s*$")
PATHISH_RE = re.compile(r"(?<![\w./~-])([A-Za-z0-9._*?+-]+(?:/[A-Za-z0-9._*?+~/-]+)+)")
BACKTICK_RE = re.compile(r"`([^`\n]{1,200})`")
MODAL_RE = re.compile(
    r"\b(must|never|always|only|do not|don't|should|use|run|install|build|test|lint|format|"
    r"prefer|avoid|required|ensure|keep|write|export|import|edit|check|before|after|"
    r"instead of|rather than)\b",
    re.I,
)
LINE_REF_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+)(?::\d+)?$")
# "the token is stored in run/settings.local.json" describes a file the app writes later, not a
# claim about what is checked in -- that is not drift, so it is reported as advisory.
RUNTIME_WRITE_RE = re.compile(
    r"\b(writ(?:e[sd]?|ten|ing)|save[sd]?|stored|creat(?:e[sd]?|ing)|generat(?:e[sd]?|ing)|"
    r"produc(?:e[sd]?|ing)|emitted|persist(?:ed|s)?|dump[sed]?|logged|export[sd]?|"
    r"record[sed]?)\b",
    re.I,
)
BANNED_RE = re.compile(
    r"\b(?:never|don'?t|do not|must not|avoid|no)\b[^.\n]{0,40}?`(?P<token>[^`\n]{2,60})`",
    re.I,
)
INSTEAD_RE = re.compile(
    r"use\s+`(?P<good>[^`]{1,60})`\s+(?:instead of|rather than|over|not)\s+`(?P<bad>[^`]{1,60})`",
    re.I,
)
EVERY_MUST_HAVE_RE = re.compile(
    r"\b(?:every|each|all|any)\b[^`\n]{0,60}?\b(?:must|should|needs? to)\s+"
    r"(?:have|contain|include|ship with|provide)\s+`?(?P<artifact>[\w.\-]+\.[a-z]{1,8})`?",
    re.I,
)

EXTRACTION_SYSTEM_PROMPT = """Extract every rule, instruction and claim from the harness files below. Reply ONLY:
{"rules":[{"id":"r1","source":"CLAUDE.md","line":14,
 "kind":"path|command|skill|mcp|convention","text":"<verbatim rule>",
 "check":{"type":"path_exists|command_runs|file_referenced|server_reachable|semantic",
          "target":"src/foo.ts"}}]}
Rules must be verbatim quotes with a real line number. If a rule cannot be checked
mechanically, use kind "convention" and check.type "semantic". Never invent a rule."""

SEMANTIC_SYSTEM_PROMPT = """You audit whether a repository follows one of its documented conventions.
You are given the rule and an evidence digest assembled from real files. Reply ONLY:
{"verdicts":[{"id":"<rule id>","verdict":"holds|violated|unknown","evidence":"<path:line + quote>"}]}
`unknown` is a valid and respectable answer when the digest cannot settle it.
Never invent a path, line number or quote."""


# ---------------------------------------------------------------- data model


@dataclass
class Rule:
    id: str
    source: str
    line: int
    kind: str
    text: str
    check: str
    target: str
    extractor: str = "static"
    occurrences: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "line": self.line, "kind": self.kind,
            "text": self.text, "check": self.check, "checkLabel": CHECK_LABELS.get(self.check, ""),
            "target": self.target, "extractor": self.extractor, "occurrences": self.occurrences,
        }


@dataclass
class Finding:
    id: str
    category: str
    check: str
    severity: str
    title: str
    message: str
    ruleText: str
    ruleFile: str
    ruleLine: int
    reality: str
    evidence: dict[str, Any]
    suggestion: str
    diff: str | None = None
    deterministic: bool = True
    verdict: str | None = None
    engine: str = "static"
    advisory: bool = False
    skipped: bool = False
    occurrences: list[str] = field(default_factory=list)
    journalRef: str = ""
    folded: int = 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.__dict__)
        out["checkLabel"] = CHECK_LABELS.get(self.check, self.check)
        return out


# ---------------------------------------------------------------- text utils


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def is_glob_pattern(token: str) -> bool:
    return bool(re.search(r"[*?\[]", token))


def looks_like_path(token: str, root_entries: set[str]) -> bool:
    """Conservative: only treat a token as a real path claim when it plausibly is one."""
    raw = (token or "").strip().strip("`")
    if not raw or raw.startswith(("~/", "$", "%", "(", "@", "#")):
        return False
    parts = PurePosixPath(raw.replace("\\", "/").rstrip("/")).parts
    if not parts or any(_is_placeholder(p) for p in parts):
        return False
    if any(p in (".", "..") for p in parts):
        return False
    head = parts[0]
    if head.lower() in ("http:", "https:"):
        return False
    if "." in head and head.rsplit(".", 1)[-1].lower() in URL_TLDS:
        return False
    if head.startswith("-") or re.fullmatch(r"[\d.]+", raw):
        return False
    if head.upper() == head and len(head) > 2 and "." not in head:
        return False
    last = parts[-1]
    if "." in last:
        ext = last.rsplit(".", 1)[-1].lower()
        if ext in KNOWN_EXTS:
            return True
        if ext.isdigit():
            return False
    elif len(parts) == 1:
        return False
    if is_glob_pattern(raw):
        return True
    return head.lower() in COMMON_TOP_DIRS or head in root_entries


def _is_placeholder(segment: str) -> bool:
    return bool(re.search(r"<[^>]*>|\{[^}]*\}|\.\.\.", segment)) or segment.lower() in PLACEHOLDER_DIRS


def split_command(command: str) -> list[str]:
    """Split a documented shell line into single commands without running anything."""
    parts = re.split(r"&&|;|\|\||\|", command)
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith("$ ") or part.startswith("> "):
            part = part[2:].strip()
        out.append(part)
    return out


def tokenize(part: str) -> tuple[list[str], str | None]:
    """Return (tokens, target-directory) for one command segment."""
    workdir: str | None = None
    text = part
    while True:
        match = re.match(r"^cd\s+(\S+)\s*(?:&&|;)?\s*(.*)$", text)
        if not match:
            break
        workdir = match.group(1).strip("'\"")
        text = match.group(2)
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    while tokens and ENV_ASSIGN_RE.match(tokens[0]):
        tokens = tokens[1:]
    return tokens, workdir


def make_diff(rel_path: str, old_text: str, new_text: str) -> str | None:
    """A unified diff `git apply` can consume. Files with no trailing newline are skipped."""
    if old_text == new_text:
        return None
    if not old_text.endswith("\n"):
        return None
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    if not new_lines:
        return None
    if not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{rel_path}",
                                     tofile=f"b/{rel_path}", n=3))
    if not diff:
        return None
    return "".join(diff)


def file_text(bundle: ToolBundle, rel_path: str) -> str:
    return bundle.read_text(rel_path) or ""


# ---------------------------------------------------------------- extraction


@dataclass
class HarnessFile:
    path: str
    lines: list[str]
    role: str = "instructions"  # "instructions" = an agent loads it; "docs" = humans read it

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def at(self, line: int) -> str:
        return self.lines[line - 1] if 1 <= line <= len(self.lines) else ""


class RuleExtractor:
    """Pull checkable rules out of harness text, each with a real line number."""

    def __init__(self, bundle: ToolBundle, harness: list[HarnessFile],
                 mcp_server_names: set[str] | None = None) -> None:
        self.bundle = bundle
        self.harness = harness
        self.root_entries = self._root_entries()
        self.mcp_server_names = mcp_server_names or set()
        self.rules: dict[tuple[str, str, str], Rule] = {}
        self.rejected: list[dict[str, Any]] = []
        self._n = 0

    def _root_entries(self) -> set[str]:
        payload = self.bundle.call("read_harness", {"paths": ["."]})
        entries: set[str] = set()
        for item in payload.get("files", []):
            for entry in item.get("entries", []) or []:
                name = entry.get("name", "")
                if name:
                    entries.add(str(name).rstrip("/"))
        return entries

    def _add(self, hf: HarnessFile, line: int, kind: str, check: str, target: str,
             text: str | None = None, extractor: str = "static") -> Rule | None:
        key = (kind, check, target)
        if key in self.rules:
            existing = self.rules[key]
            ref = f"{hf.path}:{line}"
            if ref not in existing.occurrences:
                existing.occurrences.append(ref)
            return existing
        self._n += 1
        rule = Rule(
            id=f"r{self._n}", source=hf.path, line=line, kind=kind,
            text=(text if text is not None else hf.at(line)).strip(),
            check=check, target=target, extractor=extractor,
            occurrences=[f"{hf.path}:{line}"],
        )
        self.rules[key] = rule
        return rule

    # -------------------------------------------------------------- static

    def extract_static(self) -> list[Rule]:
        for hf in self.harness:
            self._extract_file(hf)
        return list(self.rules.values())

    def _extract_file(self, hf: HarnessFile) -> None:
        fence_lang: str | None = None
        for index, raw in enumerate(hf.lines, start=1):
            fence = FENCE_RE.match(raw)
            if fence:
                fence_lang = (fence.group(1) or "").lower() if fence_lang is None else None
                continue
            in_fence = fence_lang is not None
            bashish = in_fence and fence_lang in BASH_LANGS
            if in_fence and not bashish:
                continue  # example code, not a rule
            self._extract_line(hf, index, raw, bashish=bashish, in_fence=in_fence)

    def _extract_line(self, hf: HarnessFile, line: int, raw: str, *, bashish: bool,
                      in_fence: bool) -> None:
        stripped = raw.strip()
        if not stripped:
            return

        if bashish:
            command = self._console_command(raw)
            if command:
                self._add(hf, line, "command", "command_runs", command)
                return

        for token in self._path_candidates(raw):
            # `.hermes/skills/**` is a glob, not the name of one declared skill.
            if ("/skills/" in token or token.startswith(("skills/", ".hermes/skills"))) \
                    and not is_glob_pattern(token):
                self._add(hf, line, "skill", "file_referenced", token.strip("/"))
            else:
                self._add(hf, line, "path", "path_exists", token.strip("/"))

        for span in BACKTICK_RE.findall(raw):
            span = span.strip()
            if 3 <= len(span) <= 160 and self._is_documented_command(span):
                self._add(hf, line, "command", "command_runs", span)

        for name in self._mcp_names_in_text(raw):
            self._add(hf, line, "mcp", "server_reachable", name)

        if not in_fence and len(stripped) > 24 and MODAL_RE.search(stripped):
            others = [r for r in self.rules.values() if r.source == hf.path and r.line == line
                      and r.kind != "convention"]
            # A line can claim a path AND state a convention; keep the convention when it
            # is mechanically checkable, or when nothing else on the line was captured.
            if not others or BANNED_RE.search(stripped) or INSTEAD_RE.search(stripped) \
                    or EVERY_MUST_HAVE_RE.search(stripped):
                self._add(hf, line, "convention", "semantic", stripped[:400])

    @staticmethod
    def _console_command(line: str) -> str | None:
        text = line.strip()
        if not text or text.startswith(("#", "//", "!")):
            return None
        if text.startswith("$ ") or text.startswith("> "):
            text = text[2:].strip()
        first = text.split(maxsplit=1)[0] if text else ""
        base = first.split("/")[-1]
        if base in RUNNER_NAMES or first.startswith(("./", "/")) or ENV_ASSIGN_RE.match(first):
            return text
        return None

    def _path_candidates(self, raw: str) -> list[str]:
        """Slash-paths (and extension files inside a directory) named by this line."""
        outside_backticks = BACKTICK_RE.sub(" ", raw)
        tokens = [m.group(1) for m in PATHISH_RE.finditer(outside_backticks)]
        tokens += [s.strip() for s in BACKTICK_RE.findall(raw)]
        out: list[str] = []
        for token in tokens:
            token = token.strip().strip("`").rstrip(".,;:!?)'\"")
            if "/" not in token or token in out:
                continue
            if not looks_like_path(token, self.root_entries):
                continue
            out.append(token)
        return out

    @staticmethod
    def _is_documented_command(span: str) -> bool:
        tokens = span.split(maxsplit=2)
        if not tokens:
            return False
        base = tokens[0].split("/")[-1]
        if base not in RUNNER_NAMES:
            return False
        if base in {"npm", "pnpm", "yarn", "bun"}:
            if len(tokens) < 2 or tokens[1].startswith("-"):
                return False
            if tokens[1] in {"run", "run-script", "rs"}:
                return len(tokens) >= 3
            return tokens[1] not in NO_DECL_SUBCOMMANDS
        return True

    def _mcp_names_in_text(self, raw: str) -> list[str]:
        names = [m.group(1) for m in re.finditer(r"mcp__([A-Za-z0-9_+-]+)__", raw)]
        for server in self.mcp_server_names:
            if re.search(rf"(?<![\w-]){re.escape(server)}(?![\w-])", raw):
                names.append(server)
        return list(dict.fromkeys(names))

    # -------------------------------------------------------------- LLM

    def _locate_quote(self, hf: HarnessFile, quote: str, hint: int) -> int | None:
        """Where does this quote really live? `hint` is trusted only if it checks out."""
        target = _norm(quote)
        if len(target) < 3:
            return None
        try:
            line = int(hint)
        except (TypeError, ValueError):
            line = 0
        if 1 <= line <= len(hf.lines) and _norm(hf.at(line)) and target in _norm(hf.at(line)):
            return line
        for index, text in enumerate(hf.lines, start=1):
            norm = _norm(text)
            if norm and (target in norm or norm in target):
                return index
        return None

    async def extract_llm(self, cfg: ChatConfig) -> list[Rule]:
        payload = [{"path": hf.path,
                    "numbered": "\n".join(f"{i}|{line}" for i, line in enumerate(hf.lines, 1))}
                   for hf in self.harness]
        user = json.dumps({"harnessFiles": payload}, ensure_ascii=False)
        result = await chat_json(cfg, EXTRACTION_SYSTEM_PROMPT, user)
        if not result.ok or not isinstance(result.data, dict):
            self.rejected.append({"source": "all",
                                  "reason": f"extraction call failed: {result.error}"})
            return []
        by_path = {hf.path: hf for hf in self.harness}
        accepted: list[Rule] = []
        for item in result.data.get("rules") or []:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "")
            source = next((p for p in by_path if p.lower() == source.lower()), source)
            hf = by_path.get(source)
            if hf is None:
                self.rejected.append({"source": source, "reason": "unknown source file"})
                continue
            text = str(item.get("text") or "")
            located = self._locate_quote(hf, text, item.get("line") or 0)
            if located is None:
                self.rejected.append({"source": source, "line": item.get("line"),
                                      "text": text[:120],
                                      "reason": "quote not found in the file -- fabricated rule"})
                continue
            check = str((item.get("check") or {}).get("type") or "semantic")
            if check not in set(CHECK_LABELS) - {"convention_static", "convention_llm"}:
                check = "semantic"
            kind = str(item.get("kind") or "convention")
            if kind not in {"path", "command", "skill", "mcp", "convention"}:
                kind = "convention"
            target = str((item.get("check") or {}).get("target") or "").strip() or text[:200]
            rule = self._add(hf, located, kind, check, target, text=text, extractor="llm")
            if rule is not None:
                accepted.append(rule)
        return accepted


# ---------------------------------------------------------------- manifests


@dataclass
class Manifests:
    """What the repo declares, so a documented command can be checked without running it."""

    package_jsons: list[str] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)      # name -> declaring file
    workspaces: list[str] = field(default_factory=list)
    make_targets: set[str] = field(default_factory=set)
    makefiles: list[str] = field(default_factory=list)
    just_targets: set[str] = field(default_factory=set)
    entry_points: dict[str, str] = field(default_factory=dict)  # console script -> file
    deps: set[str] = field(default_factory=set)
    pyprojects: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    path: str = ""

    def declares_script(self, name: str) -> bool:
        return name in self.scripts

    def declares_target(self, name: str) -> bool:
        return name in self.make_targets or name in self.just_targets


class ManifestReader:
    def __init__(self, bundle: ToolBundle) -> None:
        self.bundle = bundle

    def read(self) -> Manifests:
        m = Manifests(path=str(self.bundle.repo_root))
        payload = self.bundle.call("inspect_repo", {"globs": list(MANIFEST_GLOBS)})
        paths: list[str] = []
        for result in payload.get("results", []):
            for item in result.get("files", []):
                if item.get("path"):
                    paths.append(item["path"])
        for rel in dict.fromkeys(paths):
            name = PurePosixPath(rel).name
            if name == "package.json":
                m.package_jsons.append(rel)
                self._package_json(rel, m)
            elif name in {"Makefile", "makefile", "GNUmakefile"}:
                m.makefiles.append(rel)
                self._makefile(rel, m)
            elif name in {"justfile", "Justfile"}:
                self._justfile(rel, m)
            elif name == "pyproject.toml":
                m.pyprojects.append(rel)
                self._pyproject(rel, m)
            elif name.startswith("requirements"):
                m.requirements.append(rel)
                self._requirements(rel, m)
        return m

    def _lines(self, rel: str) -> list[str]:
        return (self.bundle.read_text(rel, max_bytes=600_000) or "").splitlines()

    def _package_json(self, rel: str, m: Manifests) -> None:
        raw = self.bundle.read_text(rel, max_bytes=400_000)
        if not raw:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        for name in (data.get("scripts") or {}):
            m.scripts.setdefault(str(name), rel)
        for field_name in ("dependencies", "devDependencies", "peerDependencies",
                           "optionalDependencies"):
            for dep in (data.get(field_name) or {}):
                m.deps.add(str(dep).lower())
        for entry in data.get("workspaces") or []:
            if isinstance(entry, str):
                m.workspaces.append(entry)

    def _makefile(self, rel: str, m: Manifests) -> None:
        for line in self._lines(rel):
            if line.startswith(("#", "\t", " ", "include", "-include", "export")):
                continue
            match = re.match(r"^([A-Za-z0-9_./%$()\-]+):(?![=:])", line)
            if match:
                m.make_targets.add(match.group(1))

    def _justfile(self, rel: str, m: Manifests) -> None:
        for line in self._lines(rel):
            match = re.match(r"^@?([A-Za-z0-9_\-]+)\s*(?:\([^)]*\))?\s*:", line)
            if match and not line.startswith("#"):
                m.just_targets.add(match.group(1))

    def _pyproject(self, rel: str, m: Manifests) -> None:
        raw = self.bundle.read_text(rel, max_bytes=400_000)
        if not raw:
            return
        try:
            data = tomllib.loads(raw)
        except Exception:
            self._pyproject_regex(raw, m, rel)
            return
        project = data.get("project") or {}
        for name in (project.get("scripts") or {}):
            m.entry_points.setdefault(str(name), rel)
        for dep in project.get("dependencies") or []:
            m.deps.add(_dep_name(str(dep)))
        for extras in (project.get("optional-dependencies") or {}).values():
            for dep in extras or []:
                m.deps.add(_dep_name(str(dep)))
        for group in (data.get("dependency-groups") or {}).values():
            for dep in group or []:
                if isinstance(dep, str):
                    m.deps.add(_dep_name(dep))
        tool_uv = (data.get("tool") or {}).get("uv") or {}
        for dep in tool_uv.get("dev-dependencies") or []:
            if isinstance(dep, str):
                m.deps.add(_dep_name(dep))

    def _pyproject_regex(self, raw: str, m: Manifests, rel: str) -> None:
        section = ""
        for line in raw.splitlines():
            head = line.split("#", 1)[0].strip()
            if head.startswith("[") and head.endswith("]"):
                section = head[1:-1]
                continue
            if section == "project.scripts":
                match = re.match(r"^([A-Za-z0-9_.\-]+)\s*=", head)
                if match:
                    m.entry_points.setdefault(match.group(1), rel)
            elif section == "project" and head.startswith("dependencies"):
                for dep in re.findall(r'"([^"]+)"', head):
                    m.deps.add(_dep_name(dep))

    def _requirements(self, rel: str, m: Manifests) -> None:
        for line in self._lines(rel):
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith(("-r", "-c", "--")):
                m.deps.add(_dep_name(line))


def _dep_name(raw: str) -> str:
    return re.split(r"[<>=!~;\[\s]", raw.strip(), maxsplit=1)[0].strip().lower()


# ---------------------------------------------------------------- auditor

PRIMARY_INSTRUCTION_NAMES = {
    "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENT.md", ".cursorrules", "GEMINI.md",
    "QODER.md", ".windsurfrules", "copilot-instructions.md",
}


def _is_primary_instruction(path: str) -> bool:
    """A file an agent actually loads by default, not just documentation humans read."""
    name = PurePosixPath(path).name
    return name in PRIMARY_INSTRUCTION_NAMES or name.endswith(".mdc")


def _llm_usable(cfg: ChatConfig | None) -> bool:
    """A local endpoint (LM Studio, Ollama) is usable without a key; a hosted one is not."""
    if cfg is None or not cfg.configured:
        return False
    if cfg.key_present:
        return True
    return "localhost" in cfg.base_url or "127.0.0.1" in cfg.base_url


class AuditCore:
    """Runs the five checks against one repo and produces the drift report."""

    def __init__(self, bundle: ToolBundle, cfg: ChatConfig | None = None,
                 categories: set[str] | None = None) -> None:
        self.bundle = bundle
        self.cfg = cfg
        self.categories = set(categories) if categories else None
        self.harness: list[HarnessFile] = []
        self.harness_paths: set[str] = set()
        self.roles: dict[str, str] = {}
        self._lines: dict[str, list[str]] = {}
        self._refs: dict[str, str] = {}
        self.mcp_configs: list[dict[str, Any]] = []
        self.skill_dirs: list[str] = []
        self.manifests = Manifests()
        self.rules: list[Rule] = []
        self.extractor: RuleExtractor | None = None
        self.findings: list[Finding] = []
        self.skipped: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}
        self._seen: dict[tuple[str, str, str], Finding] = {}
        self._id = 0

    # ------------------------------------------------------------------ utils

    def wanted(self, category: str) -> bool:
        return self.categories is None or category in self.categories

    def ref(self, tool: str) -> str:
        journal = self.bundle.journal
        return f"{tool}#{journal[-1].seq if journal else 0}"

    def add(self, **kwargs: Any) -> Finding | None:
        """Append a finding, folding exact repeats (same category/check/title)."""
        key = (kwargs.get("category", ""), kwargs.get("check", ""), kwargs.get("title", ""))
        if key in self._seen:
            self._seen[key].folded += 1
            return None
        self._id += 1
        kwargs.setdefault("id", f"d{self._id}")
        kwargs.setdefault("journalRef", self.ref("inspect_repo"))
        kwargs.setdefault("occurrences", [])
        kwargs.setdefault("folded", 0)
        finding = Finding(**kwargs)
        self._seen[key] = finding
        self.findings.append(finding)
        return finding

    def note_skipped(self, stage: str, reason: str, **extra: Any) -> None:
        self.skipped.append({"stage": stage, "reason": reason, **extra})

    def rule_evidence(self, rule: Rule) -> dict[str, Any]:
        lines = self._lines.get(rule.source) or []
        lo, hi = max(1, rule.line - 5), min(len(lines), rule.line + 5)
        return {
            "path": rule.source,
            "line": rule.line,
            "text": lines[rule.line - 1] if 0 < rule.line <= len(lines) else rule.text,
            "quote": f"{rule.source}:{rule.line}",
            "context": [{"line": i, "text": lines[i - 1]} for i in range(lo, hi + 1)],
            "journalRef": self._refs.get(rule.source, ""),
        }

    @staticmethod
    def reality(hit: Hit | None, label: str = "") -> dict[str, Any] | None:
        if hit is None:
            return None
        return {"path": hit.path, "line": hit.line, "text": hit.text.strip()[:400],
                "quote": f"{hit.path}:{hit.line}", "ref": hit.ref, "label": label}

    # -------------------------------------------------------------- discovery

    async def discover(self, emit: ProgressFn = None) -> None:
        await _emit(emit, "discover", "running", "enumerating harness files")
        found: dict[str, list[str]] = {}
        for role, globs in (("instructions", INSTRUCTION_GLOBS), ("mcp", MCP_GLOBS),
                            ("skills", SKILL_GLOBS), ("manifest", MANIFEST_GLOBS)):
            payload = self.bundle.call("inspect_repo", {"globs": list(globs), "heads": False})
            paths: list[str] = []
            for result in payload.get("results", []):
                for item in result.get("files", []):
                    if item.get("path"):
                        paths.append(item["path"])
            found[role] = list(dict.fromkeys(paths))
            await _emit(emit, "discover", "progress", f"{role}: {len(found[role])} file(s)")

        primary = {path for path in found["instructions"] if _is_primary_instruction(path)}
        self.harness = await self._read(found["instructions"], as_rules=True)
        self.mcp_configs = await self._read(found["mcp"], as_rules=False)
        for path in found["skills"]:
            parent = str(PurePosixPath(path).parent)
            if parent and parent != "." and parent not in self.skill_dirs:
                self.skill_dirs.append(parent)
        self.manifests = ManifestReader(self.bundle).read()

        if not primary:
            first = found["instructions"][0] if found["instructions"] else ""
            first_lines = (self.bundle.read_text(first, max_bytes=4_000) or "").splitlines() if first else []
            self.add(
                category="conventions", check="harness_missing", severity="info",
                title="No agent instruction file in the repo",
                message="None of CLAUDE.md / AGENTS.md / .cursorrules / .cursor/rules/*.mdc / "
                        "GEMINI.md exists, so there is no harness for an agent to load by default.",
                ruleText=(first_lines[0].strip() if first_lines else "(no instruction file found)")[:200],
                ruleFile=first or "CLAUDE.md", ruleLine=1,
                reality="instruction files found: " + (", ".join(found["instructions"]) or "none"),
                evidence={"path": first or ".", "line": 1,
                          "text": (first_lines[0].strip() if first_lines else ""),
                          "quote": first or ".",
                          "context": [{"line": i, "text": t}
                                      for i, t in enumerate(first_lines[:6], start=1)],
                          "journalRef": self.ref("inspect_repo")},
                suggestion="Add CLAUDE.md or AGENTS.md at the repo root if agents work in this "
                           "repo; without one there is nothing for Luthier to audit.",
                diff=None, deterministic=True, advisory=True, engine="static",
            )
        await _emit(emit, "discover", "done",
                    f"{len(self.harness)} instruction file(s), {len(self.mcp_configs)} MCP "
                    f"config(s), {len(self.skill_dirs)} skill dir(s), "
                    f"{len(self.manifests.package_jsons) + len(self.manifests.pyprojects)} manifest(s)")

    async def _read(self, paths: list[str], *, as_rules: bool) -> list[Any]:
        out: list[Any] = []
        for start in range(0, len(paths), 40):
            chunk = paths[start:start + 40]
            payload = self.bundle.call("read_harness", {"paths": chunk})
            reference = self.ref("read_harness")
            for item in payload.get("files", []):
                if not item.get("exists") or item.get("isDirectory"):
                    continue
                path = item["path"]
                lines = parse_numbered(item.get("numbered", ""))
                self._lines[path] = lines
                self._refs[path] = reference
                self.harness_paths.add(path)
                if as_rules:
                    role = "instructions" if _is_primary_instruction(path) else "docs"
                    self.roles[path] = role
                    out.append(HarnessFile(path=path, lines=lines, role=role))
                else:
                    out.append({"path": path, "lines": lines, "ref": reference})
        return out

    # -------------------------------------------------------------- extraction

    async def extract(self, emit: ProgressFn = None) -> None:
        await _emit(emit, "extract", "running", "building the rule inventory")
        server_names = set()
        for config in self.mcp_configs:
            server_names |= set(self._mcp_servers(config).keys())
        self.extractor = RuleExtractor(self.bundle, self.harness, server_names)
        static_rules = self.extractor.extract_static()
        self.rules = static_rules
        await _emit(emit, "extract", "progress",
                    f"static extractor: {len(static_rules)} rule(s) "
                    f"({sum(1 for r in static_rules if r.check == 'semantic')} convention(s))")

        llm_rules: list[Rule] = []
        if _llm_usable(self.cfg):
            llm_rules = await self.extractor.extract_llm(self.cfg)  # type: ignore[arg-type]
        else:
            self.note_skipped("rule-extraction", "skipped: no_api_key",
                              detail="static extraction only; convention rules still get the "
                                     "mechanical subset")
        self.rules = list(self.extractor.rules.values())
        self.meta["extraction"] = {
            "static": len(static_rules),
            "llmAccepted": len(llm_rules),
            "rejected": self.extractor.rejected[:40],
            "rejectedCount": len(self.extractor.rejected),
            "total": len(self.rules),
            "byKind": _count_kinds(self.rules),
        }
        await _emit(emit, "extract", "done",
                    f"inventory: {len(self.rules)} rule(s); "
                    f"fabricated quotes rejected: {len(self.extractor.rejected)}")

    def _mcp_servers(self, config: dict[str, Any]) -> dict[str, Any]:
        """Servers from a parsed MCP config, tolerant of the three known shapes."""
        text = "\n".join(config.get("lines") or [])
        data = _load_jsonc(text)
        if not isinstance(data, dict):
            return {}
        for key in MCP_SERVER_KEYS:
            value = data.get(key)
            if isinstance(value, dict):
                return {str(k): v for k, v in value.items()}
        return {}


def _count_kinds(rules: list[Rule]) -> dict[str, int]:
    out: dict[str, int] = {}
    for rule in rules:
        out[rule.kind] = out.get(rule.kind, 0) + 1
    return out


def _load_jsonc(text: str) -> Any:
    """Parse JSON, falling back to stripping // comments and trailing commas."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    stripped = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    try:
        return json.loads(stripped)
    except ValueError:
        return None


async def _emit(emit: ProgressFn, stage: str, status: str, detail: str) -> None:
    if emit is not None:
        await emit({"stage": stage, "status": status, "detail": detail})


# ---------------------------------------------------------------- the checks


class ChecksMixin:
    """The five drift checks. Mixed into Auditor."""

    # 1 ---------------------------------------------------------- path_exists

    async def check_paths(self, emit: ProgressFn = None) -> None:
        if not self.wanted("paths"):
            self.note_skipped("check-paths", "skipped: category disabled")
            return
        rules = [r for r in self.rules if r.check == "path_exists"]
        await _emit(emit, "check-paths", "running", f"verifying {len(rules)} path(s)")
        proven = self._prove_paths([r.target for r in rules])
        tree = self.bundle.tree()
        for rule in rules:
            target = rule.target
            entry = proven.get(target) or {"exists": False, "matched": 0, "files": [], "ref": ""}
            if entry["exists"]:
                continue
            doc_example = self._is_likely_doc_example(rule, target, tree)
            if is_glob_pattern(target):
                self.add(
                    category="paths", check="path_exists",
                    severity="low" if doc_example else "high",
                    title=f"No file matches the documented pattern {target}"
                          + (" (an example in prose?)" if doc_example else ""),
                    message=f"{rule.source}:{rule.line} tells the agent to work with `{target}`, "
                            f"but `inspect_repo` found 0 files matching it.",
                    ruleText=rule.text, ruleFile=rule.source, ruleLine=rule.line,
                    reality=f"glob {target} matched nothing "
                            f"(repo has {len(tree)} auditable file(s))",
                    evidence=self.rule_evidence(rule),
                    suggestion=f"Update the pattern, or restore the files it describes. "
                               f"Proven by {entry['ref'] or self.ref('inspect_repo')}.",
                    diff=None if doc_example
                    else delete_line_diff(self.bundle, rule.source, rule.line),
                    deterministic=True, engine="static", advisory=doc_example,
                )
                continue
            candidates = suggest_candidates(target, tree)
            listing = ", ".join(candidates[:4]) or "no close match"
            runtime = bool(RUNTIME_WRITE_RE.search(rule.text))
            advisory = runtime or doc_example
            self.add(
                category="paths", check="path_exists",
                severity="low" if advisory else "high",
                title=(f"{target} is written at runtime, not checked in" if runtime
                       else f"Documented path does not exist: {target}"
                            + (" (its directory is not in this repo)" if doc_example else "")),
                message=(f"{rule.source}:{rule.line} says this file is produced at run time, so "
                         f"its absence is expected -- worth naming what creates it."
                         if runtime else
                         f"{rule.source}:{rule.line} refers to `{target}` which is not on disk."),
                ruleText=rule.text, ruleFile=rule.source, ruleLine=rule.line,
                reality=f"`{target}` missing; nearest existing path(s): {listing}",
                evidence=self.rule_evidence(rule),
                suggestion=(f"Keep the path but say what creates it. Closest existing: {listing}."
                            if advisory else
                            (f"Point the rule at {candidates[0]} ({len(candidates)} candidate(s) "
                             f"found)" if candidates
                             else "Remove the stale reference or restore the file")),
                diff=None if advisory
                else rewrite_path_diff(self.bundle, rule, target, candidates),
                deterministic=True, engine="static",
                advisory=advisory,
                occurrences=rule.occurrences,
            )
        await _emit(emit, "check-paths", "done",
                    f"{sum(1 for f in self.findings if f.category == 'paths')} stale path(s)")

    def _is_likely_doc_example(self, rule: Rule, target: str, tree: list[str]) -> bool:
        """A prose doc naming a path in a directory this repo does not have.

        CLAUDE.md pointing at a missing file is drift an agent will hit. A README quoting
        `src/old.ts` from inside an unrelated example is usually just an example, so it is
        surfaced as advisory rather than charged as a failure.
        """
        if self.roles.get(rule.source, "instructions") != "docs":
            return False
        literal = re.split(r"[*?\[]", target.strip("/"))[0].strip("/")
        if not literal:
            return False
        head = literal.split("/")[0]
        return not any(p == head or p.startswith(head + "/") for p in tree)

    def _prove_paths(self, targets: list[str]) -> dict[str, dict[str, Any]]:
        """Existence for every target, batched through journaled `inspect_repo` calls."""
        out: dict[str, dict[str, Any]] = {}
        uniq = [t for t in dict.fromkeys(targets) if t]
        for start in range(0, len(uniq), 40):
            chunk = uniq[start:start + 40]
            payload = self.bundle.call("inspect_repo", {"globs": chunk, "heads": False})
            reference = self.ref("inspect_repo")
            for result in payload.get("results", []):
                files = [item.get("path") for item in result.get("files", []) if item.get("path")]
                out[result["pattern"]] = {
                    "exists": bool(result.get("exists")) and bool(files),
                    "matched": len(files), "files": files[:20], "ref": reference,
                }
        return out

    # 2 ---------------------------------------------------------- command_runs

    async def check_commands(self, emit: ProgressFn = None) -> None:
        if not self.wanted("commands"):
            self.note_skipped("check-commands", "skipped: category disabled")
            return
        rules = [r for r in self.rules if r.check == "command_runs"]
        await _emit(emit, "check-commands", "running",
                    f"checking {len(rules)} documented command(s) against declarations "
                    f"(nothing is executed)")
        for rule in rules:
            for segment in split_command(rule.target):
                verdict = classify_command(segment, self.manifests, self.bundle)
                if verdict.resolved:
                    continue
                self.add(
                    category="commands", check="command_runs",
                    severity=verdict.severity,
                    title=verdict.title,
                    message=f"`{segment}` at {rule.source}:{rule.line} -- {verdict.detail}",
                    ruleText=rule.text, ruleFile=rule.source, ruleLine=rule.line,
                    reality=verdict.reality,
                    evidence=self.rule_evidence(rule),
                    suggestion=verdict.suggestion,
                    diff=command_fix_diff(self.bundle, rule, segment, verdict),
                    deterministic=True, engine="static",
                    advisory=verdict.advisory,
                )
        await _emit(emit, "check-commands", "done",
                    f"{sum(1 for f in self.findings if f.category == 'commands')} undeclared "
                    f"command(s)")

    # 3 ------------------------------------------------------- file_referenced

    async def check_skills(self, emit: ProgressFn = None) -> None:
        if not self.wanted("skills"):
            self.note_skipped("check-skills", "skipped: category disabled")
            return
        rules = [r for r in self.rules if r.check == "file_referenced"]
        await _emit(emit, "check-skills", "running",
                    f"reconciling {len(rules)} declared skill(s) with "
                    f"{len(self.skill_dirs)} skill dir(s) on disk")

        declared = {skill_name(r.target): r for r in rules}
        proven = self._prove_paths([r.target for r in rules])
        tree = self.bundle.tree()
        for name, rule in declared.items():
            entry = proven.get(rule.target) or {"exists": False}
            doc_example = self._is_likely_doc_example(rule, rule.target, tree)
            if not entry.get("exists"):
                self.add(
                    category="skills", check="file_referenced",
                    severity="low" if doc_example else "high",
                    title=f"Declared skill directory is missing: {name}"
                          + (" (an example in prose?)" if doc_example else ""),
                    message=f"{rule.source}:{rule.line} declares the skill `{name}` at "
                            f"`{rule.target}`, which does not exist on disk.",
                    ruleText=rule.text, ruleFile=rule.source, ruleLine=rule.line,
                    reality=f"no directory or files match `{rule.target}`",
                    evidence=self.rule_evidence(rule),
                    suggestion="Restore the skill directory, or delete its declaration so the "
                               "agent stops trying to use it.",
                    diff=None if doc_example
                    else delete_line_diff(self.bundle, rule.source, rule.line),
                    deterministic=True, engine="static", advisory=doc_example,
                )
                continue
            prefix = rule.target.rstrip("/") + "/"
            outside = self.bundle.mentions(
                name,
                exclude_paths=lambda p: (p.startswith(prefix) or p.endswith("/SKILL.md")
                                         or p in self.harness_paths),
            )
            inside = self.bundle.mentions(
                name, exclude_paths=lambda p: not (p.startswith(prefix) or p.endswith("/SKILL.md")),
            )
            if not outside:
                self.add(
                    category="skills", check="file_referenced",
                    severity="low" if doc_example else "medium",
                    title=f"Skill declared but never used: {name}"
                          + (" (an example in prose?)" if doc_example else ""),
                    message=f"`{name}` is declared in the harness but no file outside the harness "
                            f"and outside its own directory mentions it.",
                    ruleText=rule.text, ruleFile=rule.source, ruleLine=rule.line,
                    reality=f"mentions inside {rule.target}: {len(inside)}; "
                            f"mentions elsewhere in the repo: 0",
                    evidence=self.rule_evidence(rule),
                    suggestion="Drop the declaration (the agent has no reason to load it), or "
                               "wire the skill into a file that actually invokes it.",
                    diff=None if doc_example
                    else delete_line_diff(self.bundle, rule.source, rule.line),
                    deterministic=True, engine="static", advisory=doc_example,
                )

        instruction_text = "\n".join(" ".join(hf.lines) for hf in self.harness).lower()
        for directory in self.skill_dirs:
            name = skill_name(directory)
            mentioned = name.lower() in instruction_text or directory.lower() in instruction_text
            if mentioned:
                continue
            contents = sorted(p for p in self.bundle.tree() if p.startswith(directory + "/"))
            cited = next((p for p in contents if PurePosixPath(p).name.lower() == "skill.md"),
                         contents[0] if contents else directory)
            cited_lines = (self.bundle.read_text(cited, max_bytes=8_000) or "").splitlines()
            self.add(
                category="skills", check="skill_unlisted", severity="medium",
                title=f"Skill directory not listed in the harness: {directory}",
                message=f"{cited} exists on disk, but no instruction file mentions `{name}`, so "
                        f"an agent reading only the harness cannot know the skill is there.",
                ruleText=(cited_lines[0].strip() if cited_lines else f"(skill: {directory})")[:200],
                ruleFile=cited, ruleLine=1,
                reality="skill content present: " + ", ".join(contents[:3])
                        + f"; mentions in {len(self.harness)} instruction file(s): 0",
                evidence={"path": cited, "line": 1,
                          "text": cited_lines[0] if cited_lines else "",
                          "quote": f"{cited}:1",
                          "context": [{"line": i, "text": t}
                                      for i, t in enumerate(cited_lines[:6], start=1)],
                          "journalRef": self.ref("inspect_repo")},
                suggestion=f"List `{directory}` in the skills section of "
                           f"{self._first_harness_path() or 'CLAUDE.md'}.",
                diff=self._add_skill_line_diff(directory, name),
                deterministic=True, engine="static",
            )
        await _emit(emit, "check-skills", "done",
                    f"{sum(1 for f in self.findings if f.category == 'skills')} skill drift(s)")

    def _first_harness_path(self) -> str:
        for candidate in ("CLAUDE.md", "AGENTS.md"):
            if candidate in self.harness_paths:
                return candidate
        return sorted(self.harness_paths)[0] if self.harness_paths else ""

    def _add_skill_line_diff(self, directory: str, name: str) -> str | None:
        target = self._first_harness_path()
        if not target:
            return None
        text = self.bundle.read_text(target) or ""
        if not text:
            return None
        lines = text.splitlines()
        heading = next((i for i, line in enumerate(lines)
                        if re.match(r"^#{1,4}\s*skill", line, re.I)), None)
        entry = f"- `{name}` -- {directory}/SKILL.md"
        if heading is None:
            new_lines = lines + ["", "## Skills", entry]
        else:
            index = heading + 1
            while index < len(lines) and not lines[index].startswith("#"):
                if lines[index].startswith("- "):
                    index += 1
                    continue
                break
            new_lines = lines[:index] + [entry] + lines[index:]
        return make_diff(target, text, "\n".join(new_lines) + "\n")

    # 4 ----------------------------------------------------- server_reachable

    async def check_mcp(self, emit: ProgressFn = None) -> None:
        if not self.wanted("mcp"):
            self.note_skipped("check-mcp", "skipped: category disabled")
            return
        await _emit(emit, "check-mcp", "running",
                    f"validating {len(self.mcp_configs)} MCP config(s); no server is spawned")
        configured: set[str] = set()
        configured_at: dict[str, tuple[str, int]] = {}
        for config in self.mcp_configs:
            path = config["path"]
            lines = config["lines"]
            text = "\n".join(lines)
            data = _load_jsonc(text)
            if data is None:
                self.add(
                    category="mcp", check="server_reachable", severity="critical",
                    title=f"MCP config is not valid JSON: {path}",
                    message=f"{path} cannot be parsed, so every server it declares is dead on "
                            f"load.",
                    ruleText=text.splitlines()[0][:200] if lines else "(empty file)",
                    ruleFile=path, ruleLine=1, reality="json parse failed",
                    evidence={"path": path, "line": 1, "text": (lines[0] if lines else ""),
                              "quote": f"{path}:1",
                              "context": [{"line": i, "text": t}
                                          for i, t in enumerate(lines[:10], start=1)],
                              "journalRef": config["ref"]},
                    suggestion="Fix the JSON (quotes, commas) or delete the file.",
                    diff=None, deterministic=True, engine="static",
                )
                continue
            servers = self._mcp_servers(config)
            if not servers:
                continue
            for name, entry in servers.items():
                configured.add(name)
                span = json_member_span(lines, name)
                line_no = span[0] if span else 1
                configured_at[name] = (path, line_no)
                evidence = {
                    "path": path, "line": line_no,
                    "text": (lines[line_no - 1].strip() if lines else ""),
                    "quote": f"{path}:{line_no}",
                    "context": [{"line": i, "text": lines[i - 1]}
                                for i in range(max(1, line_no - 5), min(len(lines), line_no + 5) + 1)],
                    "journalRef": config["ref"],
                }
                rule_text = f"{path}: MCP server `{name}`"
                problems: list[str] = []
                if not isinstance(entry, dict):
                    problems.append("entry is not an object")
                    command = None
                else:
                    command = entry.get("command")
                    if command is None and entry.get("url"):
                        continue  # remote server: no binary to resolve
                    if not isinstance(command, str) or not command.strip():
                        problems.append("no `command` and no `url`")
                        command = None
                    args = entry.get("args")
                    if args is not None and not isinstance(args, list):
                        problems.append("`args` must be a list")
                    env = entry.get("env")
                    if env is not None and not isinstance(env, dict):
                        problems.append("`env` must be an object")
                for problem in problems:
                    self.add(
                        category="mcp", check="mcp_schema", severity="high",
                        title=f"MCP server `{name}` is misconfigured: {problem}",
                        message=f"{path}:{line_no} declares `{name}` which fails schema "
                                f"validation ({problem}).",
                        ruleText=rule_text, ruleFile=path, ruleLine=line_no,
                        reality=json.dumps(entry if isinstance(entry, dict) else entry)[:300],
                        evidence=evidence,
                        suggestion="Give the entry a `command` (string) plus `args` (list of "
                                   "strings), or a `url`.",
                        diff=None, deterministic=True, engine="static",
                    )
                if command:
                    found, resolution = resolve_binary(str(command), self.bundle, self.manifests)
                    if not found:
                        candidate = closest_binary(str(command), self.bundle, self.manifests)
                        self.add(
                            category="mcp", check="server_reachable", severity="critical",
                            title=f"MCP server `{name}` cannot start: `{command}` is not "
                                  f"resolvable",
                            message=f"{path}:{line_no} launches `{name}` with `{command}`, which "
                                    f"is not on PATH and is not declared by this repo.",
                            ruleText=rule_text, ruleFile=path, ruleLine=line_no,
                            reality=resolution,
                            evidence=evidence,
                            suggestion=(f"Install `{command}` or change the entry's command."
                                        if candidate is None else
                                        f"Point the entry at `{candidate}`, which does resolve."),
                            diff=mcp_command_diff(self.bundle, path, span, str(command),
                                                  candidate),
                            deterministic=True, engine="static",
                        )
                if isinstance(entry, dict) and entry.get("disabled") is True:
                    self.add(
                        category="mcp", check="server_reachable", severity="low",
                        title=f"MCP server `{name}` is disabled",
                        message=f"{path}:{line_no} keeps `{name}` configured but disabled; the "
                                f"harness may still reference it.",
                        ruleText=rule_text, ruleFile=path, ruleLine=line_no,
                        reality="disabled: true", evidence=evidence,
                        suggestion="Remove the entry, or re-enable it and document it.",
                        diff=None, deterministic=False, advisory=True, engine="static",
                    )

        for rule in [r for r in self.rules if r.kind == "mcp" and r.check == "server_reachable"]:
            if rule.target in configured:
                continue
            self.add(
                category="mcp", check="server_reachable", severity="high",
                title=f"Harness references MCP server `{rule.target}` that is not configured",
                message=f"{rule.source}:{rule.line} tells the agent to use `{rule.target}`, but "
                        f"no MCP config in this repo declares it.",
                ruleText=rule.text, ruleFile=rule.source, ruleLine=rule.line,
                reality="configured servers: " + (", ".join(sorted(configured)) or "none"),
                evidence=self.rule_evidence(rule),
                suggestion=f"Add a `{rule.target}` entry to .mcp.json, or drop the reference.",
                diff=delete_line_diff(self.bundle, rule.source, rule.line),
                deterministic=True, engine="static",
            )

        instruction_text = "\n".join(" ".join(hf.lines) for hf in self.harness).lower()
        for name in sorted(configured):
            mentioned = name.lower() in instruction_text
            if mentioned:
                continue
            where = configured_at.get(name) or (self._first_harness_path() or ".mcp.json", 1)
            cfg_lines = self._lines.get(where[0]) or []
            cited_text = cfg_lines[where[1] - 1] if 0 < where[1] <= len(cfg_lines) else ""
            self.add(
                category="mcp", check="mcp_undocumented", severity="info",
                title=f"MCP server `{name}` is configured but undocumented",
                message=f"{where[0]}:{where[1]} makes `{name}` available to every MCP client, but "
                        f"no instruction file mentions it, so agents will not know to use it.",
                ruleText=f"(configured MCP server: {name})",
                ruleFile=where[0], ruleLine=where[1],
                reality="no mention in any instruction file: "
                        + (", ".join(hf.path for hf in self.harness) or "none"),
                evidence={"path": where[0], "line": where[1], "text": cited_text.strip(),
                          "quote": f"{where[0]}:{where[1]}",
                          "context": [{"line": i, "text": t}
                                      for i, t in enumerate(cfg_lines[:12], start=1)],
                          "journalRef": self.ref("grep")},
                suggestion=f"Document `{name}` in CLAUDE.md / AGENTS.md when it is meant to be "
                           f"used.",
                diff=None, deterministic=True, advisory=True, engine="static",
            )
        await _emit(emit, "check-mcp", "done",
                    f"{sum(1 for f in self.findings if f.category == 'mcp')} MCP drift(s)")

    # 5 ------------------------------------------------------------- semantic

    async def check_conventions(self, emit: ProgressFn = None) -> None:
        if not self.wanted("conventions"):
            self.note_skipped("check-conventions", "skipped: category disabled")
            return
        rules = [r for r in self.rules
                 if r.check == "semantic" or (r.kind == "convention" and r.check == "semantic")]
        await _emit(emit, "check-conventions", "running",
                    f"judging {len(rules)} convention rule(s)")
        pending: list[Rule] = []
        for rule in rules:
            outcome = mechanical_convention(rule, self)
            if outcome is None:
                pending.append(rule)
                continue
            verdict, reality_hit, detail = outcome
            if verdict == "holds":
                continue
            self.add(
                category="conventions", check="convention_static", severity="medium",
                title=f"Convention violated: {detail['summary']}",
                message=f"{rule.source}:{rule.line} says \"{clip(rule.text, 180)}\" -- "
                        f"{detail['why']}",
                ruleText=rule.text, ruleFile=rule.source, ruleLine=rule.line,
                reality=detail["reality"],
                evidence=self.rule_evidence(rule),
                suggestion=detail["suggestion"],
                diff=detail.get("diff"),
                deterministic=False, verdict=verdict, engine="static",
                advisory=self.roles.get(rule.source, "instructions") == "docs",
            )
            if reality_hit is not None:
                last = self.findings[-1]
                last.evidence["reality"] = self.reality(reality_hit, "violating code")
        await _emit(emit, "check-conventions", "progress",
                    f"mechanical subset settled {len(rules) - len(pending)} of {len(rules)}")

        if not pending:
            await _emit(emit, "check-conventions", "done", "no rules needed the model")
            return
        if not _llm_usable(self.cfg):
            self.note_skipped("check-conventions-llm", "skipped: no_api_key",
                              rules=len(pending))
            self.add(
                category="conventions", check="convention_llm", severity="info",
                title=f"{len(pending)} convention(s) left unjudged: skipped: no_api_key",
                message="These rules cannot be settled mechanically and no inference endpoint is "
                        "configured, so they are reported as unjudged rather than clean.",
                ruleText="; ".join(clip(r.text, 60) for r in pending[:6]) or "(none)",
                ruleFile=pending[0].source, ruleLine=pending[0].line,
                reality=f"{len(pending)} rule(s) awaiting a model verdict",
                evidence=self.rule_evidence(pending[0]),
                suggestion="Set OPENAI_API_KEY (or point Settings at LM Studio / Ollama) to judge "
                           "them.",
                diff=None, deterministic=False, skipped=True, engine="llm",
                verdict="unknown",
                occurrences=[f"{r.source}:{r.line}" for r in pending],
            )
            await _emit(emit, "check-conventions", "done",
                        f"{len(pending)} convention(s) skipped: no_api_key")
            return

        verdicts = await semantic_llm(self, pending, emit)
        await _emit(emit, "check-conventions", "done",
                    f"LLM judged {len(verdicts)} rule(s)")

    async def check_agent_narrative(self, emit: ProgressFn = None) -> None:
        """Placeholder filled by main.py: the Strands loop's own account of the audit."""
        return None


# ---------------------------------------------------------------- diff builders


def clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def skill_name(target: str) -> str:
    parts = [p for p in (target or "").replace("\\", "/").strip("/").split("/") if p]
    if not parts:
        return ""
    if parts[-1].upper() in {"SKILL.MD", "SKILL.JSON", "INDEX.MD", "README.MD"} and len(parts) > 1:
        parts = parts[:-1]
    return parts[-1]


def suggest_candidates(target: str, tree: list[str]) -> list[str]:
    """Plausible replacements for a stale path, from the real file list."""
    target = target.strip("/")
    base = PurePosixPath(target).name
    same_name = [p for p in tree if PurePosixPath(p).name == base]
    if same_name:
        return same_name
    parent = str(PurePosixPath(target).parent)
    siblings = [p for p in tree if str(PurePosixPath(p).parent) == parent][:5]
    ext = PurePosixPath(target).suffix
    fuzzy = [p for p in difflib.get_close_matches(target, tree, n=4, cutoff=0.7)
             if not ext or PurePosixPath(p).suffix == ext]
    same_ext_here = [p for p in tree if str(PurePosixPath(p).parent) == parent
                     and p.endswith(ext)] if ext and ext != target else []
    ordered: list[str] = []
    for group in (fuzzy, same_ext_here, siblings):
        for path in group:
            if path not in ordered:
                ordered.append(path)
    return ordered[:6]


def _patched_lines(bundle: ToolBundle, path: str, mutate: Callable[[list[str]], list[str]]):
    text = bundle.read_text(path, max_bytes=600_000)
    if text is None:
        return None
    lines = text.splitlines()
    new_lines = mutate(list(lines))
    if new_lines == lines:
        return None
    return make_diff(path, text, "\n".join(new_lines) + "\n")


def delete_line_diff(bundle: ToolBundle, path: str, line: int) -> str | None:
    """Drop the offending harness line."""
    if not path or line < 1:
        return None

    def mutate(lines: list[str]) -> list[str]:
        if line > len(lines):
            return lines
        return lines[: line - 1] + lines[line:]

    return _patched_lines(bundle, path, mutate)


def replace_token_diff(bundle: ToolBundle, path: str, line: int, old: str, new: str) -> str | None:
    if not path or not old or not new or line < 1:
        return None

    def mutate(lines: list[str]) -> list[str]:
        if line > len(lines) or old not in lines[line - 1]:
            return lines
        lines[line - 1] = lines[line - 1].replace(old, new, 1)
        return lines

    return _patched_lines(bundle, path, mutate)


def rewrite_path_diff(bundle: ToolBundle, rule: Rule, target: str,
                      candidates: list[str]) -> str | None:
    """Rename to the closest real path, else drop the stale claim from the harness line.

    A rename is only proposed when the file name itself is unchanged (a move), or the two
    paths are near-identical. Anything looser goes in the suggestion, not in a diff.
    """
    for candidate in candidates[:1]:
        base = PurePosixPath(target.strip("/")).name
        same_name = base == PurePosixPath(candidate).name
        near_identical = difflib.SequenceMatcher(None, target, candidate).ratio() >= 0.86
        if (same_name or near_identical) and bundle.exists(candidate):
            diff = replace_token_diff(bundle, rule.source, rule.line, target.strip("/"), candidate)
            if diff:
                return diff
    # A compound line ("read README.md, then docs/arch.md") or a line that also states a
    # convention still holds valid content, so only excise the stale path there. A line that
    # exists solely to name the stale path goes.
    text = bundle.read_text(rule.source, max_bytes=600_000)
    if text is None:
        return None
    lines = text.splitlines()
    if not 0 < rule.line <= len(lines):
        return None
    line = lines[rule.line - 1]
    tokens = [t.strip().strip("`") for t in BACKTICK_RE.findall(line)]
    tokens += PATHISH_RE.findall(BACKTICK_RE.sub(" ", line))
    siblings = [t for t in tokens
                if t.strip("/") != target.strip("/") and looks_like_path(t, set())]
    # If removing the claim leaves a sentence with nothing checkable in it, the line was
    # only ever there to name that path -- delete it rather than leave a fragment.
    cleaned = strip_token_from_line(line, target)
    survives = [t for t in [x.strip().strip("`") for x in BACKTICK_RE.findall(cleaned)]
                + PATHISH_RE.findall(BACKTICK_RE.sub(" ", cleaned))
                if looks_like_path(t, set()) or _is_documented_command_token(t)]
    if (siblings or survives) and cleaned.strip():
        return strip_token_diff(bundle, rule.source, rule.line, target)
    return delete_line_diff(bundle, rule.source, rule.line)


def _is_documented_command_token(token: str) -> bool:
    base = (token or "").split(maxsplit=1)[0].split("/")[-1] if token else ""
    return base in RUNNER_NAMES


def strip_token_from_line(line: str, token: str) -> str:
    """The line with one stale path claim removed, sentence tidied."""
    quoted, bare = f"`{token}`", token.strip("/")
    removed = line.replace(quoted, "", 1) if quoted in line else line.replace(bare, "", 1)
    cleaned = re.sub(r"\s{2,}", " ", removed)
    # A verb or connective left dangling in front of punctuation is residue of the removal.
    cleaned = re.sub(r"\s*\b(?:use|see|run|via|from|with|in|at|to|and|then|or|plus)\b"
                     r"\s*([.;,:])", r"\1", cleaned, flags=re.I)
    cleaned = re.sub(r"[,;]\s*([.;,])", r"\1", cleaned)
    cleaned = re.sub(r"\s+([.;,:])", r"\1", cleaned)
    cleaned = re.sub(r"\(\s*[,;]?\s*\)", "()", cleaned)
    cleaned = re.sub(r"\s+(?:use|see|and|then|or)\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^(\s*[-*]\s*),?\s*$", "", cleaned)
    return cleaned


def strip_token_diff(bundle: ToolBundle, path: str, line: int, token: str) -> str | None:
    """Remove one stale path claim, leaving the rest of the sentence intact."""

    def mutate(lines: list[str]) -> list[str]:
        if line > len(lines):
            return lines
        cleaned = strip_token_from_line(lines[line - 1], token)
        if cleaned.strip() in {"", "-", "*", ".", "and", "then", "or"}:
            return lines[: line - 1] + lines[line:]
        lines[line - 1] = cleaned
        return lines

    return _patched_lines(bundle, path, mutate)


# ---------------------------------------------------------------- binaries


def path_env(bundle: ToolBundle, manifests: Manifests | None = None) -> str:
    root = Path(bundle.repo_root)
    dirs = [str(root / "node_modules" / ".bin"), str(root / ".venv" / "bin"),
            str(root / "venv" / "bin"), str(root / ".direnv" / "bin"),
            str(root / "bin"), str(root / "scripts")]
    for package in (manifests.package_jsons if manifests else []):
        dirs.append(str(root / Path(package).parent / "node_modules" / ".bin"))
    home = Path.home()
    dirs += [str(home / ".local" / "bin"), str(home / ".cargo" / "bin"), str(home / "bin"),
             "/opt/homebrew/bin", "/usr/local/bin"]
    return os.pathsep.join(dict.fromkeys([os.environ.get("PATH", "")] + dirs))


def resolve_binary(command: str, bundle: ToolBundle, manifests: Manifests) -> tuple[bool, str]:
    """Does the documented binary resolve? PATH + local bin dirs + repo declarations. Never run."""
    command = command.strip()
    if not command:
        return False, "empty command"
    if "/" in command:
        rel = command.lstrip("./")
        if bundle.exists(rel):
            return True, f"file exists at {rel}"
        return False, f"no such file: {rel}"
    found = shutil.which(command, path=path_env(bundle, manifests))
    if found:
        return True, f"resolves to {found}"
    lowered = command.lower()
    if lowered in manifests.deps:
        return True, "declared as a dependency in a manifest"
    if command in manifests.entry_points:
        return True, f"declared entry point in {manifests.entry_points[command]}"
    for package in manifests.package_jsons:
        node_bin = Path(bundle.repo_root) / Path(package).parent / "node_modules" / ".bin" / command
        if node_bin.exists():
            return True, f"installed at {node_bin.relative_to(Path(bundle.repo_root))}"
    return False, (f"`{command}` is not on PATH, not installed under node_modules/.bin, and not "
                   f"declared by package.json / pyproject.toml / requirements*.txt")


def _path_binaries(bundle: ToolBundle, manifests: Manifests) -> list[str]:
    names: list[str] = []
    for directory in dict.fromkeys(path_env(bundle, manifests).split(os.pathsep)):
        try:
            names.extend(sorted(p.name for p in Path(directory).iterdir() if p.is_file()))
        except OSError:
            continue
    return list(dict.fromkeys(names))


def closest_binary(command: str, bundle: ToolBundle, manifests: Manifests) -> str | None:
    matches = difflib.get_close_matches(command, _path_binaries(bundle, manifests), n=1,
                                        cutoff=0.82)
    return matches[0] if matches else None


@dataclass
class CommandVerdict:
    resolved: bool
    severity: str = "high"
    title: str = ""
    detail: str = ""
    reality: str = ""
    suggestion: str = ""
    replace: tuple[str, str] | None = None
    advisory: bool = False


def classify_command(segment: str, manifests: Manifests, bundle: ToolBundle) -> CommandVerdict:
    """Verify a documented command is *declared*. Nothing is ever executed."""
    tokens, workdir = tokenize(segment)
    if not tokens:
        return CommandVerdict(True)
    first = tokens[0]
    base = first.split("/")[-1]

    if base in {"sudo", "time", "env", "nohup", "yes", "echo", "printf", "cd", "export", "set"}:
        return CommandVerdict(True)

    if base == "git":
        return CommandVerdict(True) if shutil.which("git") else CommandVerdict(
            False, "medium", "`git` is not resolvable", "git is documented but not resolvable",
            resolve_binary("git", bundle, manifests)[1], "Install git or drop the instruction.")

    # script invocations: verify the file the runner is pointed at
    if base in {"bash", "sh", "zsh", "python", "python3", "node", "ruby", "perl", "deno", "bun"}:
        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            if "/" in token or re.search(r"\.(sh|bash|py|js|mjs|cjs|ts|rb|pl)$", token):
                rel = token.lstrip("./")
                if not bundle.exists(rel):
                    candidates = suggest_candidates(rel, bundle.tree())
                    return CommandVerdict(
                        False, "high", f"Documented script file is missing: {rel}",
                        f"{segment} runs `{token}`, which is not in the repo",
                        f"no file at {rel}; nearest: " + (", ".join(candidates[:3]) or "none"),
                        "Restore the script or point the harness at the real path.",
                        (token, candidates[0]) if candidates else None,
                    )
                break
        resolved, why = resolve_binary(base, bundle, manifests)
        return CommandVerdict(resolved, "medium", f"`{base}` is not resolvable",
                              f"{base} is required by the documented command", why,
                              f"Install {base} or document a different runner.")

    if base in {"npm", "pnpm", "yarn", "bun"}:
        resolved, why = resolve_binary(base, bundle, manifests)
        if not resolved:
            return CommandVerdict(False, "high", f"`{base}` is not resolvable",
                                  f"{segment} needs the `{base}` runner", why,
                                  f"Install {base} or change the documented package manager.")
        if len(tokens) < 2:
            return CommandVerdict(True)
        sub = tokens[1]
        if sub in {"-v", "--version", "--help", "-h"}:
            return CommandVerdict(True)
        if sub in NO_DECL_SUBCOMMANDS:
            return CommandVerdict(True)
        if sub in {"run", "run-script", "rs"}:
            if len(tokens) < 3:
                return CommandVerdict(True)
            script = tokens[2]
        elif sub.startswith("-"):
            return CommandVerdict(True)
        else:
            script = sub
        if script in NO_DECL_SUBCOMMANDS:
            return CommandVerdict(True)
        if not manifests.package_jsons:
            return CommandVerdict(
                False, "high", f"`{base} run {script}` has no package.json to declare it",
                f"{segment} calls script `{script}` but the repo has no package.json",
                "no package.json found in the repo",
                "Add the script to package.json, or document a command the repo can run.")
        if manifests.declares_script(script):
            return CommandVerdict(True)
        names = sorted(manifests.scripts)
        close = difflib.get_close_matches(script, names, n=1, cutoff=0.7)
        declared = ", ".join(f"`{name}`" for name in names[:10]) or "(none declared)"
        return CommandVerdict(
            False, "high", f"Script `{script}` is not declared in package.json",
            f"{base} run {script} is documented, but no package.json in the repo declares "
            f"`{script}`",
            f"declared scripts: {declared}",
            (f"Change the harness to `{close[0]}`." if close
             else f"Declare `{script}` in package.json or drop the instruction."),
            (script, close[0]) if close else None,
        )

    if base in {"make", "gmake"}:
        targets = [t for t in tokens[1:] if not t.startswith("-")]
        if not manifests.makefiles:
            if targets:
                return CommandVerdict(False, "high", "`make` is documented but there is no "
                                                     "Makefile",
                                      f"{segment} runs make, but no Makefile exists in the repo",
                                      "no Makefile / makefile / GNUmakefile found",
                                      "Add a Makefile or document the real build command.")
            resolved, why = resolve_binary("make", bundle, manifests)
            return CommandVerdict(resolved, "medium", "`make` is not resolvable", "", why, "")
        target = targets[0] if targets else ""
        if target and target not in manifests.make_targets:
            close = difflib.get_close_matches(target, sorted(manifests.make_targets), n=1,
                                             cutoff=0.7)
            return CommandVerdict(
                False, "high", f"Make target `{target}` is not declared",
                f"{segment} runs target `{target}`",
                "targets in " + ", ".join(manifests.makefiles) + ": "
                + (", ".join(sorted(manifests.make_targets)[:12]) or "(none)"),
                (f"Use `{close[0]}`." if close else "Declare the target or drop the instruction."),
                (target, close[0]) if close else None,
            )
        resolved, why = resolve_binary("make", bundle, manifests)
        return CommandVerdict(resolved, "medium", "`make` is not resolvable", segment, why, "")

    if base in {"just", "task"}:
        files = ([p for p in bundle.tree(200)
                  if PurePosixPath(p).name.lower() in {"justfile", "taskfile.yml"}])
        targets = [t for t in tokens[1:] if not t.startswith("-")]
        if targets and not files:
            return CommandVerdict(False, "medium", f"`{base}` recipe file is missing",
                                  f"{segment} uses {base}", f"no {base}file found",
                                  f"Add the {base}file or change the documented command.")
        return CommandVerdict(True)

    if base in {"npx", "bunx", "uvx", "deno"}:
        package = next((t for t in tokens[1:] if not t.startswith("-")), "")
        if package:
            clean = re.split(r"[@/]", package, maxsplit=1)[0]
            installed = (bundle.exists(f"node_modules/{package}")
                         or bundle.exists(f"node_modules/.bin/{clean}"))
            if installed or clean.lower() in manifests.deps:
                return CommandVerdict(True)
            return CommandVerdict(
                False, "low", f"`{base} {package}` is not declared",
                f"{segment} fetches `{package}` at run time",
                "not present in node_modules and not declared in a manifest",
                f"Declare `{package}` as a devDependency so the command is reproducible.",
                advisory=True,
            )
        return CommandVerdict(True)

    if base in {"python", "python3"} and len(tokens) > 2 and tokens[1] == "-m":
        module = tokens[2]
        if bundle.exists(f"src/{module}") or bundle.exists(f"{module}") or \
                bundle.exists(f"{module.replace('.', '/')}.py") or module.lower() in manifests.deps:
            return CommandVerdict(True)
        return CommandVerdict(
            False, "medium", f"`python -m {module}` names nothing this repo declares",
            f"{segment} imports module `{module}` at run time",
            "no matching module, package or dependency declaration",
            f"Install/declare `{module}` or correct the documented invocation.",
        )

    if base in RUNNER_NAMES:
        resolved, why = resolve_binary(base, bundle, manifests)
        if resolved:
            return CommandVerdict(True)
        return CommandVerdict(False, "medium", f"`{base}` is not resolvable",
                              f"{segment} needs `{base}`", why,
                              f"Install `{base}` or document a tool the repo actually uses.")

    # unknown runner: only flag it when it is clearly a local script path
    if first.startswith(("./", "../", "/")):
        rel = first.lstrip("./")
        if bundle.exists(rel):
            return CommandVerdict(True)
        return CommandVerdict(False, "high", f"Documented script is missing: {rel}",
                              f"{segment} executes `{first}`", "no such file in the repo",
                              "Restore the script or update the harness.")
    return CommandVerdict(True)


def command_fix_diff(bundle: ToolBundle, rule: Rule, segment: str,
                     verdict: CommandVerdict) -> str | None:
    if not verdict.replace:
        return None
    old, new = verdict.replace
    return replace_token_diff(bundle, rule.source, rule.line, old, new)


# ---------------------------------------------------------------- MCP plumbing


def json_member_span(lines: list[str], name: str) -> tuple[int, int] | None:
    """1-based inclusive line span of `"name": <value>` in a JSON-ish file."""
    pattern = re.compile(r'"%s"\s*:' % re.escape(name))
    start = next((i for i, line in enumerate(lines) if pattern.search(line)), None)
    if start is None:
        return None
    match = pattern.search(lines[start])
    text = "\n".join(lines)
    offset = sum(len(lines[i]) + 1 for i in range(start)) + (match.end() if match else 0)
    while offset < len(text) and text[offset] in " \t\r\n":
        offset += 1
    if offset >= len(text) or text[offset] not in "{[":
        return (start + 1, start + 1)
    closer = {"{": "}", "[": "]"}[text[offset]]
    depth = 0
    in_string = escaped = False
    for index in range(offset, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                end_line = text.count("\n", 0, index) + 1
                return (start + 1, min(end_line, len(lines)))
    return (start + 1, len(lines))


def mcp_command_diff(bundle: ToolBundle, path: str, span: tuple[int, int] | None,
                     command: str, candidate: str | None) -> str | None:
    """Swap a dead command for one that resolves, or drop the whole server block."""
    text = bundle.read_text(path, max_bytes=600_000)
    if text is None or span is None or not text.endswith("\n"):
        return None
    lines = text.splitlines()
    start, end = span
    if candidate:
        for index in range(start - 1, min(end, len(lines))):
            if not re.search(r'"command"\s*:', lines[index]):
                continue
            new_line, count = re.subn(
                r'("command"\s*:\s*")%s(")' % re.escape(command),
                r"\g<1>%s\g<2>" % candidate, lines[index], count=1)
            if count and new_line != lines[index]:
                lines[index] = new_line
                return make_diff(path, text, "\n".join(lines) + "\n")
        return None
    remaining = lines[: start - 1] + lines[end:]
    previous = next((i for i in range(start - 2, -1, -1) if lines[i].strip()), None)
    following = next((i for i in range(end, len(lines)) if lines[i].strip()), None)
    if previous is not None and following is not None:
        stripped = lines[previous].rstrip()
        if stripped.endswith(",") and lines[following].lstrip().startswith(closer_char(lines[end - 1])):
            remaining[previous] = stripped[:-1]
    patched = "\n".join(remaining) + "\n"
    try:
        json.loads(patched)
    except ValueError:
        return None
    return make_diff(path, text, patched)


def closer_char(line: str) -> str:
    return "}" if "{" in line else "]"


# ---------------------------------------------------------------- conventions

COMMENT_RE = re.compile(r"^\s*(?:[#/]{1,2}|\*|<!--|-->|rem |::)")


def _code_token(token: str) -> str | None:
    token = (token or "").strip().strip("`").strip(".,;:!")
    if not 2 <= len(token) <= 60:
        return None
    if token.startswith(("/", "~", "$", "-")):
        return None
    if "/" in token and "." in token:
        return None  # a path, already covered by check 1
    if " " in token and not re.search(r"[()=<>{}]", token):
        return None  # prose is not safe to grep literally
    return token


def _token_pattern(token: str) -> str:
    escaped = re.escape(token)
    escaped = escaped.replace(r"\ ", r"\s*")
    if re.fullmatch(r"[\w.$_]+", token):
        return r"(?<![\w.])%s(?![\w])" % escaped
    return escaped


def _source_only(auditor: "Auditor") -> Callable[[str], bool]:
    harness = auditor.harness_paths

    def keep(path: str) -> bool:
        if path in harness:
            return False
        ext = PurePosixPath(path).suffix.lstrip(".").lower()
        if ext in DOC_EXTS or path.endswith((".lock", "-lock.json", ".min.js", ".svg")):
            return False
        return True

    return keep


def mechanical_convention(rule: Rule,
                          auditor: "Auditor") -> tuple[str, Hit | None, dict[str, Any]] | None:
    """Settle a convention without a model, when the rule is mechanically checkable."""
    text = rule.text
    banned = BANNED_RE.search(text)
    instead = INSTEAD_RE.search(text)
    token: str | None = None
    framing = ""
    if instead:
        token, framing = _code_token(instead.group("bad")), "prefer"
    elif banned:
        token, framing = _code_token(banned.group("token")), "ban"
    if token:
        hits = [h for h in auditor.bundle.grep(_token_pattern(token), regex=True,
                                               restrict=_source_only(auditor))
                if not COMMENT_RE.match(h.text)]
        if not hits:
            return ("holds", None, {"summary": f"no `{token}` found in source",
                                    "why": f"no occurrence of `{token}` outside comments and docs",
                                    "reality": "0 occurrences in source files",
                                    "suggestion": "Nothing to fix -- the code follows this rule."})
        hit = hits[0]
        return ("violated", hit, {
            "summary": f"`{token}` is documented as disallowed but appears in the code",
            "why": (f"`{token}` is used {len(hits)} time(s) in source "
                    f"({'preferred' if framing == 'prefer' else 'banned'} context)"),
            "reality": f"{hit.path}:{hit.line}: {hit.text.strip()[:200]}"
                       + (f" -- plus {len(hits) - 1} more occurrence(s)" if len(hits) > 1 else ""),
            "suggestion": f"Replace `{token}` where it is used, or amend the rule to match the "
                          f"code.",
            "diff": None,
        })

    every = EVERY_MUST_HAVE_RE.search(text)
    if every:
        artifact = every.group("artifact").strip("/")
        directory = next((t for t in _rule_path_tokens(rule, auditor) if not is_glob_pattern(t)),
                         "")
        units = _enumerable_units(auditor, directory)
        if units:
            missing = [u for u in units if not any(
                PurePosixPath(p).name.lower() == artifact.lower()
                for p in auditor.bundle.tree() if p.startswith(u + "/"))]
            if not missing:
                return ("holds", None, {"summary": f"every unit has {artifact}",
                                        "why": "all enumerated directories contain the artifact",
                                        "reality": f"{len(units)} unit(s) checked",
                                        "suggestion": "Nothing to fix."})
            return ("violated", None, {
                "summary": f"some units lack the required `{artifact}`",
                "why": f"{len(missing)} of {len(units)} enumerated unit(s) have no `{artifact}`",
                "reality": "missing in: " + ", ".join(sorted(missing)[:6]),
                "suggestion": f"Add `{artifact}` to " + ", ".join(sorted(missing)[:3]),
                "diff": None,
            })
    return None


def _rule_path_tokens(rule: Rule, auditor: "Auditor") -> list[str]:
    return [t for t in PATHISH_RE.findall(rule.text)
            if looks_like_path(t, auditor.extractor.root_entries if auditor.extractor else set())]


def _enumerable_units(auditor: "Auditor", directory: str) -> list[str]:
    if directory and "skill" in directory.lower():
        directory = directory.strip("/")
    if directory:
        prefix = directory.strip("/") + "/"
        return sorted({str(PurePosixPath(p).parent) for p in auditor.bundle.tree()
                       if p.startswith(prefix) and str(PurePosixPath(p).parent) != prefix.rstrip("/")})
    return sorted(auditor.skill_dirs)


# ---------------------------------------------------------- LLM semantic pass


def repo_digest(auditor: "Auditor", limit: int = 9_000) -> dict[str, Any]:
    tree = auditor.bundle.tree()
    manifests = auditor.manifests
    return {
        "fileCount": len(tree),
        "files": tree[:400],
        "truncated": len(tree) > 400,
        "packageJsonScripts": sorted(manifests.scripts)[:60],
        "makeTargets": sorted(manifests.make_targets)[:60],
        "pythonDeps": sorted(manifests.deps)[:60],
        "skillDirs": auditor.skill_dirs[:60],
        "mcpServers": sorted({name for config in auditor.mcp_configs
                              for name in auditor._mcp_servers(config)}),
        "harnessFiles": [hf.path for hf in auditor.harness],
    }


async def semantic_llm(auditor: "Auditor", pending: list[Rule], emit: ProgressFn = None,
                       batch: int = 12) -> list[dict[str, Any]]:
    """Ask the model for holds/violated/unknown on the rules code cannot settle."""
    digest = repo_digest(auditor)
    verified = {p for p in auditor.bundle.tree()}
    verdicts: list[dict[str, Any]] = []
    for start in range(0, len(pending), batch):
        chunk = pending[start:start + batch]
        payload = {
            "rules": [{"id": r.id, "source": r.source, "line": r.line, "text": r.text}
                      for r in chunk],
            "repoDigest": digest,
        }
        if len(json.dumps(payload)) > 120_000:
            payload["repoDigest"]["files"] = payload["repoDigest"]["files"][:120]
        user = json.dumps(payload, ensure_ascii=False)
        await _emit(emit, "check-conventions", "progress",
                    f"LLM judging rules {start + 1}-{start + len(chunk)} of {len(pending)}")
        result = await chat_json(auditor.cfg, SEMANTIC_SYSTEM_PROMPT, user)  # type: ignore[arg-type]
        if not result.ok or not isinstance(result.data, dict):
            auditor.note_skipped("check-conventions-llm",
                                 f"skipped: inference failed -- {clip(str(result.error), 160)}",
                                 rules=len(pending) - len(verdicts))
            auditor.add(
                category="conventions", check="convention_llm", severity="info",
                title="LLM convention pass failed",
                message=f"The endpoint did not return usable verdicts, so {len(chunk)} rule(s) "
                        f"are reported as unjudged rather than clean.",
                ruleText=clip(chunk[0].text, 200), ruleFile=chunk[0].source, ruleLine=chunk[0].line,
                reality=clip(str(result.error or result.body), 400),
                evidence=auditor.rule_evidence(chunk[0]),
                suggestion="Check Settings -> Test connection, then re-run.",
                diff=None, deterministic=False, skipped=True, engine="llm", verdict="unknown",
            )
            return verdicts
        by_id = {r.id: r for r in chunk}
        for item in result.data.get("verdicts") or []:
            if not isinstance(item, dict):
                continue
            rule = by_id.get(str(item.get("id")))
            if rule is None:
                continue
            raw_verdict = str(item.get("verdict") or "unknown").lower()
            evidence_text = str(item.get("evidence") or "")
            reference = LINE_REF_RE.match(evidence_text.split(" ")[0].strip("`")) \
                if evidence_text else None
            cited_path = reference.group("file") if reference else ""
            cited_line = int(reference.group("line")) if reference else 0
            grounded = bool(cited_path) and any(
                cited_path == path or cited_path in path or path.endswith(cited_path)
                for path in verified)
            verdict = raw_verdict if raw_verdict in {"holds", "violated", "unknown"} else "unknown"
            if verdict == "violated" and not grounded:
                verdict = "unknown"
            verdicts.append({"id": rule.id, "verdict": verdict, "evidence": evidence_text,
                             "grounded": grounded})
            if verdict == "holds":
                continue
            reality_hit = None
            if grounded and cited_line:
                lines = auditor.bundle.read_lines(cited_path, cited_line)
                reality_hit = Hit(path=cited_path, line=cited_line,
                                  text=str(lines.get("citedText") or "").strip(),
                                  ref=lines.get("journalRef", ""), regex_match=None)
            auditor.add(
                category="conventions", check="convention_llm",
                severity="medium" if verdict == "violated" else "low",
                title=(f"Convention violated: {clip(rule.text, 90)}" if verdict == "violated"
                       else f"Convention unverifiable: {clip(rule.text, 90)}"),
                message=(f"{rule.source}:{rule.line} -- "
                         + ("the model says the code does not follow it."
                            if verdict == "violated" else
                            "neither the code nor the model can settle this.")),
                ruleText=rule.text, ruleFile=rule.source, ruleLine=rule.line,
                reality=clip(evidence_text, 400) or (
                    "no evidence returned" if verdict == "violated" else
                    "model answered unknown"),
                evidence=auditor.rule_evidence(rule),
                suggestion=("Fix the code or amend the rule; the finding quotes the offending "
                            "line above." if verdict == "violated"
                            else "Make the convention mechanically checkable (name the files or "
                                 "command it applies to)."),
                diff=None, deterministic=False, verdict=verdict, engine="llm",
            )
            if reality_hit is not None:
                auditor.findings[-1].evidence["reality"] = auditor.reality(
                    reality_hit, "code cited by the model")
            if verdict == "unknown" and not grounded and evidence_text:
                auditor.findings[-1].evidence["unverifiedCitation"] = clip(evidence_text, 200)
    return verdicts


# ---------------------------------------------------------------- orchestrator

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


class Auditor(AuditCore, ChecksMixin):
    """One audit run: discover, extract, five checks, score."""

    async def run(self, emit: ProgressFn = None) -> dict[str, Any]:
        await self.discover(emit)
        await self.extract(emit)
        await self.check_paths(emit)
        await self.check_commands(emit)
        await self.check_skills(emit)
        await self.check_mcp(emit)
        await self.check_conventions(emit)
        return self.report()

    def report(self) -> dict[str, Any]:
        ordered = sorted(self.findings,
                         key=lambda f: (SEVERITY_RANK.get(f.severity, 5), f.category, f.id))
        kept: list[Finding] = []
        per_check: dict[str, int] = {}
        for finding in ordered:
            count = per_check.get(finding.check, 0)
            if count >= MAX_FINDINGS_PER_CHECK:
                if kept:
                    for existing in kept:
                        if existing.check == finding.check:
                            existing.folded += 1
                            break
                continue
            per_check[finding.check] = count + 1
            kept.append(finding)

        dictionaries = [f.to_dict() for f in kept]
        score = compute_score(dictionaries)
        journal = self.bundle.journal
        by_tool: dict[str, dict[str, Any]] = {}
        for entry in journal:
            bucket = by_tool.setdefault(entry.tool, {"calls": 0, "bytes": 0, "errors": 0})
            bucket["calls"] += 1
            bucket["bytes"] += entry.bytes_returned
            bucket["errors"] += 1 if entry.error else 0
        return {
            "repo": {
                "path": str(self.bundle.repo_root),
                "files": len(self.bundle.tree()),
                "harnessFiles": [hf.path for hf in self.harness],
                "mcpConfigs": [config["path"] for config in self.mcp_configs],
                "skillDirs": self.skill_dirs,
                "manifests": {
                    "packageJson": self.manifests.package_jsons,
                    "scripts": sorted(self.manifests.scripts),
                    "makeTargets": sorted(self.manifests.make_targets),
                    "pyproject": self.manifests.pyprojects,
                    "requirements": self.manifests.requirements,
                    "deps": sorted(self.manifests.deps)[:80],
                },
            },
            "extraction": self.meta.get("extraction", {}),
            "rules": [r.to_dict() for r in self.rules],
            "findings": dictionaries,
            "skipped": self.skipped,
            "score": score["score"],
            "scoring": score,
            "constants": scoring_constants(),
            "toolCalls": {
                "total": len(journal),
                "bytesReturned": sum(e.bytes_returned for e in journal),
                "byTool": by_tool,
                "logPath": str(self.bundle.audit_log_path) if self.bundle.audit_log_path else None,
            },
        }
