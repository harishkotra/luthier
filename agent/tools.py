"""Deterministic, read-only repo access for the Luthier audit agent.

Every byte the auditor knows about the repo arrives through `ToolBundle` -- either
directly or via the Strands `@tool` wrappers in :func:`build_strands_tools`. Each call
is written to `audit.log` as JSONL and retained in an in-memory journal, so a finding can
only cite evidence that was actually read back.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MAX_BYTES_PER_FILE_DEFAULT = 20_000
INSPECT_BYTE_BUDGET = 160_000
HEAD_LINES = 20
MAX_GLOB_MATCHES = 500
MAX_WALKED_DIRS = 20_000

# Directories never worth auditing. Exact-name match so harness dirs like `.hermes`
# or `.claude` stay reachable.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    ".nuxt",
    ".turbo",
    ".parcel-cache",
    ".vite",
    ".gradle",
    ".idea",
    ".vscode-server",
    "dist",
    "out",
    "build",
    "target",
    "site-packages",
    ".eggs",
}
SKIP_PREFIXES = ("__pycache__", ".cache", ".aws-sam", "dist-", ".luthier")

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4", ".mov", ".so", ".dylib",
    ".dll", ".exe", ".class", ".pyc", ".wasm", ".beep", ".boop",
}

LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")

# Grep corpus limits. Text files below CORPUS_MAX_FILE_BYTES are read into memory once
# per run; `grep()` is journaled so any quoted line traces back to a real read.
CORPUS_MAX_FILES = 6_000
CORPUS_MAX_BYTES = 40_000_000
CORPUS_MAX_FILE_BYTES = 400_000
MAX_GREP_HITS = 3_000

# A file counts as text when the extension says so, it has no extension (Makefile, Dockerfile),
# or a probe of its first bytes looks like text -- and never when it is a known binary.
TEXT_EXTS = {
    ".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".pyi", ".go", ".rs", ".java",
    ".kt", ".rb", ".php", ".cs", ".swift", ".scala", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".css", ".scss", ".sass", ".less", ".html", ".htm", ".vue", ".svelte", ".astro",
    ".sql", ".graphql", ".gql", ".proto", ".tf", ".hcl", ".gradle", ".cmake", ".mk",
    ".bat", ".cmd", ".pl", ".lua", ".r", ".jl", ".ex", ".exs", ".dart", ".vue",
}


@dataclass
class Hit:
    """One matched line, with the journal reference that produced it."""

    path: str
    line: int
    text: str
    ref: str
    regex_match: re.Match | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "text": self.text, "ref": self.ref}


def parse_numbered(numbered: str) -> list[str]:
    """Turn a `lineno|text` payload back into a 1-based list of lines."""
    if not numbered:
        return []
    out: list[str] = []
    for index, raw in enumerate(numbered.split("\n"), start=1):
        prefix, sep, rest = raw.partition("|")
        if sep and prefix.isdigit() and int(prefix) == len(out) + 1:
            out.append(rest)
        else:
            out.append(rest if sep else raw)
    return out


def _is_textish(rel_path: str, path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in BINARY_EXTS:
        return False
    if suffix in TEXT_EXTS:
        return True
    if suffix == "":
        return True
    try:
        with path.open("rb") as handle:
            probe = handle.read(1024)
    except OSError:
        return False
    if b"\0" in probe:
        return False
    return sum(1 for byte in probe if byte in (9, 10, 13, 32) or 32 <= byte < 127) > len(probe) * 0.85


class ToolError(Exception):
    """Raised when a tool cannot honour a request; surfaced verbatim to the model."""


@dataclass
class JournalEntry:
    seq: int
    tool: str
    args: dict[str, Any]
    duration_ms: float
    bytes_returned: int
    result: dict[str, Any]
    error: str | None = None


@dataclass
class ToolBundle:
    """Repo-rooted, byte-capped reader with a JSONL call log."""

    repo_root: Path
    max_bytes_per_file: int = MAX_BYTES_PER_FILE_DEFAULT
    audit_log_path: Path | None = None
    run_id: str | None = None
    on_call: Callable[[JournalEntry], None] | None = None
    journal: list[JournalEntry] = field(default_factory=list)
    _root: Path = field(init=False, repr=False, default=None)  # type: ignore[assignment]
    _seq: int = field(init=False, default=0, repr=False)
    _corpus: dict[str, list[str]] | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        root = Path(self.repo_root).expanduser()
        if not root.is_dir():
            raise ToolError(f"repo root is not a directory: {root}")
        self._root = root.resolve()
        if self.audit_log_path is not None:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths

    def rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self._root).as_posix()
        except ValueError:
            return path.resolve().as_posix()

    def _resolve_inside(self, raw: str) -> Path:
        candidate = Path(os.path.expanduser(str(raw)))
        target = candidate if candidate.is_absolute() else self._root / candidate
        # resolve(strict=False) collapses `..` and symlinks so the containment check
        # cannot be fooled by a crafted relative path.
        resolved = target.resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise ToolError(f"path escapes the audited repo: {raw}")
        return resolved

    def _skipped(self, name: str) -> bool:
        return name in SKIP_DIRS or name.startswith(SKIP_PREFIXES)

    # ------------------------------------------------------------------ reads

    def _read_numbered(self, path: Path) -> dict[str, Any]:
        """Return file contents as `lineno|text` rows, honouring the byte cap."""
        try:
            size = path.stat().st_size
        except OSError as exc:
            return {"path": self.rel(path), "exists": True, "error": f"stat failed: {exc}"}
        if path.suffix.lower() in BINARY_EXTS:
            return {"path": self.rel(path), "exists": True, "sizeBytes": size, "binary": True,
                    "note": "binary file; contents not read"}
        budget = min(self.max_bytes_per_file, size)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                raw = handle.read(budget)
        except OSError as exc:
            return {"path": self.rel(path), "exists": True, "error": f"read failed: {exc}"}
        rows = LINE_SPLIT_RE.split(raw)
        if rows and rows[-1] == "" and raw.endswith(("\n", "\r")):
            rows.pop()
        if budget < size and raw and not raw.endswith(("\n", "\r")):
            rows = rows[:-1]
        numbered = "\n".join(f"{i}|{text}" for i, text in enumerate(rows, start=1))
        out: dict[str, Any] = {
            "path": self.rel(path),
            "exists": True,
            "sizeBytes": size,
            "lineCount": len(rows),
            "numbered": numbered,
        }
        if budget < size:
            out["truncated"] = True
            out["truncatedAtLine"] = len(rows)
            out["bytesReturned"] = len(numbered.encode())
        return out

    def read_harness(self, paths: list[str]) -> dict[str, Any]:
        """Read harness/instruction files and return their contents with line numbers.

        Line numbers are 1-based and encoded in the `numbered` payload as `lineno|text`.
        Anything reported as existing here was genuinely opened from disk.
        """
        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            raise ToolError("read_harness needs at least one path")
        if len(paths) > 200:
            raise ToolError("read_harness accepts at most 200 paths per call")

        files: list[dict[str, Any]] = []
        missing: list[str] = []
        for raw in paths:
            path = self._resolve_inside(raw)
            if not path.exists():
                missing.append(self.rel(path))
                files.append({"path": self.rel(path), "exists": False})
                continue
            if path.is_dir():
                entries = sorted(
                    (p for p in path.iterdir() if not self._skipped(p.name)),
                    key=lambda p: (p.is_dir(), p.name),
                )[:400]
                files.append({
                    "path": self.rel(path),
                    "exists": True,
                    "isDirectory": True,
                    "entries": [
                        {"name": (self.rel(p) if p.is_dir() else p.name),
                         "directory": p.is_dir(),
                         "sizeBytes": None if p.is_dir() else p.stat().st_size}
                        for p in entries
                    ],
                })
                continue
            files.append(self._read_numbered(path))
        return {
            "tool": "read_harness",
            "requested": list(paths),
            "fileCount": len(files),
            "missing": missing,
            "files": files,
        }

    def _iter_matching(self, pattern: str) -> tuple[list[Path], bool]:
        """Glob relative to the repo root; returns (matches, capped)."""
        norm = pattern.strip()
        # Strip ./ prefixes only (lstrip would also eat the dot of ".mcp.json").
        while norm.startswith("./"):
            norm = norm[2:]
        norm = norm.lstrip("/")
        absolute = Path(norm) if Path(norm).is_absolute() else (self._root / norm)
        plain = str(absolute)
        if not re.search(r"[*?\[]", plain):
            path = Path(plain)
            if path.exists():
                return ([path] if path.is_file() else sorted(
                    p for p in path.rglob("*")
                    if p.is_file() and not self._skipped_dir(p)
                )), False
            return [], False
        matches: list[Path] = []
        capped = False
        for path in sorted(Path(self._root).glob(norm)):
            if self._skipped_dir(path):
                continue
            if not path.is_file():
                continue
            matches.append(path)
            if len(matches) >= MAX_GLOB_MATCHES:
                capped = True
                break
        return matches, capped

    def _skipped_dir(self, path: Path) -> bool:
        try:
            parts = path.resolve().relative_to(self._root).parts
        except ValueError:
            return True
        return any(part in SKIP_DIRS or part.startswith(SKIP_PREFIXES) for part in parts[:-1])

    def inspect_repo(self, globs: list[str], heads: bool = True) -> dict[str, Any]:
        """Check which files exist for each glob, with sizes and the first 20 lines.

        Use this before asserting anything about the repo's real contents. Returned
        `head` rows are `lineno|text`; `truncated` reports byte- or count-limited output.
        `heads=False` (internal use only) skips content so the byte budget cannot
        truncate an existence check.
        """
        if isinstance(globs, str):
            globs = [globs]
        if not globs:
            raise ToolError("inspect_repo needs at least one glob")
        if len(globs) > 60:
            raise ToolError("inspect_repo accepts at most 60 globs per call")

        budget = INSPECT_BYTE_BUDGET
        used = 0
        results: list[dict[str, Any]] = []
        truncated = False
        for pattern in globs:
            try:
                matches, capped = self._iter_matching(pattern)
            except OSError as exc:
                results.append({"pattern": pattern, "error": f"glob failed: {exc}", "matched": 0})
                continue
            if capped:
                truncated = True
            entries: list[dict[str, Any]] = []
            for path in matches:
                if used >= budget:
                    truncated = True
                    break
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                rel = self.rel(path)
                if path.suffix.lower() in BINARY_EXTS:
                    entries.append({"path": rel, "sizeBytes": size, "binary": True})
                    used += len(rel) + 24
                    continue
                payload = self._read_numbered(path)
                head = payload.get("numbered", "") if heads else ""
                keep = head[: budget - used] if budget - used > 0 else ""
                if len(keep) < len(head):
                    keep = keep[: keep.rfind("\n") + 1]
                    truncated = True
                used += len(keep.encode()) + len(rel) + 24
                entry: dict[str, Any] = {
                    "path": rel,
                    "sizeBytes": size,
                    "lineCount": payload.get("lineCount"),
                    "head": keep,
                }
                if payload.get("truncated") or len(keep) < len(head):
                    entry["headTruncated"] = True
                if payload.get("binary"):
                    entry["binary"] = True
                if payload.get("error"):
                    entry["error"] = payload["error"]
                entries.append(entry)
            results.append({
                "pattern": pattern,
                "exists": bool(matches),
                "matched": len(entries),
                "matchCountOnDisk": len(matches) if not capped else f">={len(matches)}",
                "files": entries,
            })
        return {
            "tool": "inspect_repo",
            "requested": list(globs),
            "bytesUsed": used,
            "byteBudget": budget,
            "truncated": truncated,
            "truncationNote": (
                f"output capped at {budget} bytes; re-run with narrower globs for detail"
                if truncated else "no truncation"
            ),
            "results": results,
        }

    # ------------------------------------------------------------- bookkeeping

    def call(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run a tool, log the call to audit.log, and keep the result in the journal."""
        impl = {"read_harness": self.read_harness, "inspect_repo": self.inspect_repo}[tool_name]
        started = time.perf_counter()
        error: str | None = None
        try:
            result = impl(**args)
        except (ToolError, OSError) as exc:
            error = str(exc)
            # Keep the payload shape stable so callers never have to special-case errors.
            result = {
                "tool": tool_name,
                "error": error,
                "requested": dict(args).get("paths") or dict(args).get("globs") or [],
                "files": [],
                "results": [],
                "missing": [],
                "truncated": False,
            }
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        payload = json.dumps(result, ensure_ascii=False, default=str)
        self._seq += 1
        entry = JournalEntry(
            seq=self._seq,
            tool=tool_name,
            args=args,
            duration_ms=duration_ms,
            bytes_returned=len(payload.encode()),
            result=result,
            error=error,
        )
        self.journal.append(entry)
        if self.audit_log_path is not None:
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                "runId": self.run_id,
                "seq": entry.seq,
                "tool": tool_name,
                "args": args,
                "durationMs": duration_ms,
                "bytesReturned": entry.bytes_returned,
                "error": error,
            }
            with self.audit_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self.on_call is not None:
            self.on_call(entry)
        return result

    def internal_call(self, tool_name: str, args: dict[str, Any], result: dict[str, Any]) -> JournalEntry:
        """Journal a deterministic read that is not exposed to the model.

        The two model-facing tools stay `read_harness` / `inspect_repo`; grep-style
        corpus scans are deterministic reads, but they are logged the same way so every
        quoted line in a finding traces back to a real read.
        """
        self._seq += 1
        payload = json.dumps(result, ensure_ascii=False, default=str)
        entry = JournalEntry(
            seq=self._seq,
            tool=tool_name,
            args=args,
            duration_ms=0.0,
            bytes_returned=len(payload.encode()),
            result=result,
        )
        self.journal.append(entry)
        if self.audit_log_path is not None:
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                "runId": self.run_id,
                "seq": entry.seq,
                "tool": tool_name,
                "internal": True,
                "args": args,
                "durationMs": 0.0,
                "bytesReturned": entry.bytes_returned,
                "error": None,
            }
            with self.audit_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return entry

    def read_text(self, rel_path: str, max_bytes: int | None = None) -> str | None:
        """Read one file's text, or None if unreadable. Used for evidence quoting."""
        try:
            path = self._resolve_inside(rel_path)
        except ToolError:
            return None
        if not path.is_file():
            return None
        budget = min(max_bytes or self.max_bytes_per_file * 4, path.stat().st_size)
        try:
            return path.open("r", encoding="utf-8", errors="replace").read(budget)
        except OSError:
            return None

    # ------------------------------------------------------------------ corpus

    def _ensure_corpus(self) -> dict[str, list[str]]:
        if self._corpus is not None:
            return self._corpus
        texts: dict[str, list[str]] = {}
        started = time.perf_counter()
        bytes_read = 0
        skipped_for_budget = 0
        for rel in self.tree():
            if len(texts) >= CORPUS_MAX_FILES or bytes_read >= CORPUS_MAX_BYTES:
                skipped_for_budget += 1
                continue
            path = self._root / rel
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > CORPUS_MAX_FILE_BYTES:
                continue
            if not _is_textish(rel, path):
                continue
            text = self.read_text(rel, max_bytes=CORPUS_MAX_FILE_BYTES)
            if text is None:
                continue
            texts[rel] = LINE_SPLIT_RE.split(text)
            bytes_read += len(text.encode())
        self._corpus = texts
        self.internal_call("scan_repo", {"purpose": "grep corpus"}, {
            "tool": "scan_repo",
            "filesRead": len(texts),
            "bytesRead": bytes_read,
            "durationMs": round((time.perf_counter() - started) * 1000, 3),
            "capped": bool(skipped_for_budget),
            "limits": {"maxFiles": CORPUS_MAX_FILES, "maxBytes": CORPUS_MAX_BYTES,
                       "maxFileBytes": CORPUS_MAX_FILE_BYTES},
            "note": ("corpus capped; grep results cover the files read before the cap"
                     if skipped_for_budget else "whole repo read"),
        })
        return texts

    def grep(self, pattern: str, *, regex: bool = True, ignore_case: bool = False,
             restrict: Callable[[str], bool] | None = None) -> list[Hit]:
        """Search real file contents. Every query is journaled, so a quoted line is traceable."""
        texts = self._ensure_corpus()
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(pattern, flags) if regex else None
        needle = pattern.lower()
        hits: list[Hit] = []
        for rel in sorted(texts):
            if restrict is not None and not restrict(rel):
                continue
            for index, line in enumerate(texts[rel], start=1):
                match = compiled.search(line) if compiled else (needle in line.lower())
                if not match:
                    continue
                hits.append(Hit(
                    path=rel, line=index, text=line.rstrip("\r"),
                    regex_match=match if isinstance(match, re.Match) else None,
                    ref=f"grep#{self._seq + 1}",
                ))
                if len(hits) >= MAX_GREP_HITS:
                    break
            if len(hits) >= MAX_GREP_HITS:
                break
        self.internal_call("grep", {"pattern": pattern, "regex": regex,
                                    "ignoreCase": ignore_case}, {
            "tool": "grep",
            "pattern": pattern,
            "matched": len(hits),
            "filesMatched": len({h.path for h in hits}),
            "capped": len(hits) >= MAX_GREP_HITS,
            "corpusFiles": len(texts),
        })
        return hits

    def mentions(self, token: str, *, exclude_paths: Callable[[str], bool] | None = None) -> list[Hit]:
        """Case-insensitive literal search -- used for skill/server reference counting."""
        hits = self.grep(re.escape(token), regex=True, ignore_case=True)
        if exclude_paths is None:
            return hits
        return [hit for hit in hits if not exclude_paths(hit.path)]

    def read_lines(self, rel_path: str, line: int, context: int = 5) -> dict[str, Any]:
        """A cited line plus its neighbours, straight from disk, journaled."""
        payload = self.call("read_harness", {"paths": [rel_path]})
        entry = payload["files"][0] if payload["files"] else {}
        if not entry.get("exists"):
            return {"path": rel_path, "exists": False,
                    "journalRef": f"read_harness#{self._seq}", "context": []}
        lines = parse_numbered(entry.get("numbered", ""))
        lo, hi = max(1, line - context), min(len(lines), line + context)
        return {
            "path": rel_path,
            "exists": True,
            "lineCount": len(lines),
            "citedLine": line,
            "citedText": lines[line - 1] if 1 <= line <= len(lines) else None,
            "startLine": lo,
            "endLine": hi,
            "context": [{"line": i, "text": lines[i - 1]} for i in range(lo, hi + 1)],
            "truncated": bool(entry.get("truncated")),
            "journalRef": f"read_harness#{self._seq}",
        }

    def exists(self, rel_path: str) -> bool:
        """Existence of a repo-relative file, backed by a journaled `inspect_repo` call."""
        payload = self.call("inspect_repo", {"globs": [rel_path]})
        for result in payload.get("results", []):
            if result.get("pattern") == rel_path:
                return bool(result.get("exists"))
        return False

    def matches(self, rel_path: str) -> list[str]:
        """Repo-relative paths matching a glob, via a journaled `inspect_repo` call."""
        payload = self.call("inspect_repo", {"globs": [rel_path]})
        out: list[str] = []
        for result in payload.get("results", []):
            out.extend(entry["path"] for entry in result.get("files", []) if entry.get("path"))
        return out

    def latest(self, tool_name: str, pattern: str) -> JournalEntry | None:
        """Newest journal entry from `tool_name` whose payload mentions `pattern`."""
        for entry in reversed(self.journal):
            if entry.tool != tool_name:
                continue
            if pattern in json.dumps(entry.result, ensure_ascii=False, default=str):
                return entry
        return None

    def exists_on_disk(self, rel_path: str) -> bool:
        path = self._root / rel_path
        return path.is_file() and not self._skipped_dir(path)

    def tree(self, limit: int = 6000) -> list[str]:
        """Relative paths of every auditable file in the repo (skip-listed dirs excluded)."""
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if not self._skipped(d)]
            base = Path(dirpath)
            for name in sorted(filenames):
                if self._skipped(name):
                    continue
                path = base / name
                if self._skipped_dir(path):
                    continue
                out.append(self.rel(path))
                if len(out) >= limit:
                    return out
        return sorted(out)


def build_strands_tools(bundle: ToolBundle) -> list[Any]:
    """Expose the bundle's two readers as Strands `@tool`s bound to one repo."""
    from strands import tool

    @tool
    def read_harness(paths: list[str]) -> dict:
        """Read harness/instruction files (CLAUDE.md, AGENTS.md, .cursorrules, skills,
        MCP config, README) and return their real contents with 1-based line numbers.

        Results are encoded as `lineno|text`. A file reported as missing really is
        missing; nothing here is inferred.

        Args:
            paths: Repo-relative paths of harness files or directories to read
        """
        return bundle.call("read_harness", {"paths": list(paths)})

    @tool
    def inspect_repo(globs: list[str]) -> dict:
        """Ground truth about the repository: which files match each glob, their sizes,
        and the first 20 lines of each match. Call this before claiming anything about
        what the repo contains. Output is byte-capped and says so when truncated.

        Args:
            globs: Glob patterns relative to the repo root, e.g. ["src/**/*.ts", "Makefile"]
        """
        return bundle.call("inspect_repo", {"globs": list(globs)})

    return [read_harness, inspect_repo]


def matches_glob(rel_path: str, pattern: str) -> bool:
    """Cheap glob test against a relative path, `**` aware."""
    norm = pattern.strip()
    while norm.startswith("./"):
        norm = norm[2:]
    if fnmatch.fnmatch(rel_path, norm):
        return True
    if "**" in norm:
        prefix, _, suffix = norm.partition("**")
        if suffix and fnmatch.fnmatch(Path(rel_path).name, suffix):
            return not prefix or rel_path.startswith(prefix.strip("/"))
    return False
