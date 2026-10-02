"""`python -m agent.verify` -- prove Luthier detects seeded drift and spares a clean repo.

Two synthetic repos are written into a temp dir and audited by the real pipeline:

1. a drifted fixture (`src/old.ts` that does not exist, an unreferenced skill directory, an
   MCP server whose binary is bogus, a violated convention, an undeclared npm script), and
2. a clean fixture that must score >= 95 with zero deterministic failures.

Then: every finding's citation must be a real line, every fix diff must survive
`git apply --check`, and every finding must carry non-empty evidence.

Exit code is non-zero if any assertion fails.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .rules import Auditor
from .tools import ToolBundle

ROOT = Path(__file__).resolve().parent.parent
AUDIT_LOG = ROOT / "audit.log"

DRIFTED: dict[str, str] = {
    "CLAUDE.md": """# Project instructions

Read `README.md` before anything else.

- Edit `src/old.ts` when the CLI output changes.
- The `Button` component is in `src/components/Button.tsx`.
- Shared helpers live in `src/util.ts`.
- Always run `npm run build` before committing.
- Use `npm run checks` to validate a change.
- Deploy with the `drift-note` skill in `.hermes/skills/drift-note/`.
- Never use `console.log` in committed code.
""",
    ".hermes/skills/drift-note/SKILL.md": """---
name: drift-note
description: Record a harness drift note
---

Write the drift note into notes.md.
""",
    ".hermes/skills/ghost/SKILL.md": """---
name: ghost
description: A skill directory the harness never lists
---

This skill exists on disk but nothing in CLAUDE.md mentions it.
""",
    ".mcp.json": """{
  "mcpServers": {
    "notes": {
      "command": "notes-mcp-binary",
      "args": ["serve"],
      "env": {"NOTES_HOME": "./notes"}
    }
  }
}
""",
    "package.json": """{
  "name": "drifted-fixture",
  "version": "1.0.0",
  "scripts": {
    "build": "tsc -p .",
    "test": "node --test"
  }
}
""",
    "src/app.ts": """import { formatOutput } from './util.ts';

export function main(argv: string[]): string {
  console.log('starting', argv.length);
  return formatOutput(argv.join(' '));
}
""",
    "src/util.ts": """export function formatOutput(text: string): string {
  return text.trim();
}
""",
    # The harness still points at src/components/Button.tsx; the file moved here.
    "src/ui/Button.tsx": """export function Button({ label }: { label: string }): string {
  return label;
}
""",
    "README.md": """# Drifted fixture

A tiny TypeScript project.

Build with `npm run build`.
""",
}

CLEAN: dict[str, str] = {
    "CLAUDE.md": """# Project instructions

Read `README.md` first.

- The entry point is `src/app.ts`; shared helpers are in `src/util.ts`.
- Build with `npm run build` and test with `npm test`.
""",
    "package.json": """{
  "name": "clean-fixture",
  "version": "1.0.0",
  "scripts": {
    "build": "tsc -p .",
    "test": "node --test"
  }
}
""",
    "src/app.ts": """import { formatOutput } from './util.ts';

export function main(argv: string[]): string {
  return formatOutput(argv.join(' '));
}
""",
    "src/util.ts": """export function formatOutput(text: string): string {
  return text.trim();
}
""",
    "README.md": """# Clean fixture

A small TypeScript project with a harness that matches it.
""",
}


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def git_setup(root: Path) -> None:
    for args in (["init", "-q"], ["config", "user.email", "luthier@example.invalid"],
                 ["config", "user.name", "Luthier Verify"], ["add", "-A"],
                 ["commit", "-qm", "fixture"]):
        subprocess.run(["git", *args], cwd=root, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)


async def audit(root: Path, run_id: str) -> dict[str, Any]:
    bundle = ToolBundle(repo_root=root, audit_log_path=AUDIT_LOG, run_id=run_id)
    report = await Auditor(bundle, cfg=None).run()
    report["_repo"] = str(root)
    report["_journal"] = [{"seq": e.seq, "tool": e.tool, "args": e.args,
                           "bytesReturned": e.bytes_returned} for e in bundle.journal]
    return report


class Verifier:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            self.failures.append(name)
            print(f"  FAIL  {name}")
        if detail:
            for line in detail.splitlines():
                print(f"        {line}")

    def section(self, title: str) -> None:
        print(f"\n{title}")


def find_finding(report: dict[str, Any], *, file: str | None = None,
                 line: int | None = None, checks: tuple[str, ...] = (),
                 needle: str | None = None) -> dict[str, Any] | None:
    for finding in report["findings"]:
        if checks and finding["check"] not in checks:
            continue
        if file is not None and finding["ruleFile"] != file:
            continue
        if line is not None and finding["ruleLine"] != line:
            continue
        if needle is not None:
            # Match the claim only. Searching reality/suggestion/diff would let unrelated
            # findings match: a "nearest existing path" list quotes other findings' files.
            haystack = " ".join([str(finding.get("title", "")),
                                  str(finding.get("ruleText", ""))]).lower()
            if needle.lower() not in haystack:
                continue
        return finding
    return None


def cited_line_is_real(repo: Path, finding: dict[str, Any]) -> bool:
    path = repo / str(finding["ruleFile"])
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    number = int(finding["ruleLine"])
    return 1 <= number <= len(lines)


def evidence_ok(repo: Path, finding: dict[str, Any]) -> bool:
    """Non-empty evidence that quotes a real file (spec 8.4)."""
    evidence = finding.get("evidence") or {}
    if not isinstance(evidence, dict) or not evidence:
        return False
    if not str(evidence.get("quote", "")).strip():
        return False
    candidates = [str(evidence.get("path") or "")]
    reality = evidence.get("reality")
    if isinstance(reality, dict):
        candidates.append(str(reality.get("path") or ""))
    grounded = [path for path in candidates if path and (repo / path).is_file()]
    if not grounded:
        return False
    has_body = bool(str(evidence.get("text", "")).strip()) or bool(evidence.get("context")) \
        or isinstance(reality, dict)
    return bool(has_body)


def git_applies(repo: Path, diff: str) -> bool:
    probe = repo / ".luthier-apply-check.patch"
    probe.write_text(diff, encoding="utf-8")
    try:
        result = subprocess.run(["git", "apply", "--check", str(probe)], cwd=repo,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    finally:
        probe.unlink(missing_ok=True)
    return result.returncode == 0


async def main() -> int:
    print("Luthier verification")
    print(f"repo root for audit.log: {ROOT}")
    if AUDIT_LOG.exists():
        AUDIT_LOG.unlink()

    workspace = Path(tempfile.mkdtemp(prefix="luthier-verify-"))
    drifted = workspace / "drifted"
    clean = workspace / "clean"
    for path in (drifted, clean):
        path.mkdir(parents=True, exist_ok=True)
    write_tree(drifted, DRIFTED)
    write_tree(clean, CLEAN)
    git_setup(drifted)
    git_setup(clean)

    v = Verifier()
    drifted_report = await audit(drifted, "verify-drifted")
    clean_report = await audit(clean, "verify-clean")

    # ---- 1. seeded drift is detected, with the right file:line -----------------
    v.section("1. seeded drift in the drifted fixture")
    old_line = next(i for i, text in enumerate(DRIFTED["CLAUDE.md"].splitlines(), start=1)
                    if "src/old.ts" in text)
    stale = find_finding(drifted_report, checks=("path_exists",), needle="src/old.ts")
    v.check("stale path src/old.ts flagged", stale is not None,
            f"expected CLAUDE.md:{old_line}")
    v.check("stale path cites CLAUDE.md:<line>",
            bool(stale) and stale["ruleFile"] == "CLAUDE.md" and stale["ruleLine"] == old_line,
            f"got {stale['ruleFile']}:{stale['ruleLine']}" if stale else "no finding")

    ghost_line = next(i for i, text in enumerate(DRIFTED["CLAUDE.md"].splitlines(), start=1)
                      if "drift-note" in text)
    unreferenced = find_finding(drifted_report, checks=("file_referenced",), needle="drift-note")
    v.check("declared-but-unused skill flagged", unreferenced is not None,
            f"expected CLAUDE.md:{ghost_line}")
    v.check("unused skill cites CLAUDE.md:<line>",
            bool(unreferenced) and unreferenced["ruleFile"] == "CLAUDE.md"
            and unreferenced["ruleLine"] == ghost_line,
            f"got {unreferenced['ruleFile']}:{unreferenced['ruleLine']}" if unreferenced
            else "no finding")

    unlisted = find_finding(drifted_report, checks=("skill_unlisted",), needle="ghost")
    v.check("on-disk skill missing from the harness flagged", unlisted is not None,
            f"evidence: {json.dumps((unlisted or {}).get('evidence', {}), default=str)[:160]}"
            if unlisted else "no finding")

    bogus = find_finding(drifted_report, checks=("server_reachable",), needle="notes-mcp-binary")
    v.check("bogus MCP binary flagged", bogus is not None)
    v.check("dead MCP server cites .mcp.json",
            bool(bogus) and bogus["ruleFile"] == ".mcp.json",
            f"got {bogus['ruleFile']}:{bogus['ruleLine']}" if bogus else "no finding")

    violated = find_finding(drifted_report, checks=("convention_static", "convention_llm"),
                           needle="console.log")
    v.check("violated convention flagged", violated is not None,
            f"evidence: {json.dumps((violated or {}).get('evidence', {}), default=str)[:200]}"
            if violated else "no finding (conventions are LLM-judged; without a key this is "
                             "expected to be reported as unjudged, not clean)")
    if violated:
        evidence = violated.get("evidence", {})
        code = evidence.get("reality") or {}
        v.check("convention finding quotes the offending code",
                bool(str(violated.get("reality", ""))) and "src/app.ts" in str(code.get("path", "")),
                f"reality={violated.get('reality')!r} evidence.reality={json.dumps(code, default=str)}")
        v.check("convention quote is a real line in the cited file",
                cited_line_is_real(drifted, {"ruleFile": code.get("path"),
                                              "ruleLine": code.get("line")}))

    undeclared = find_finding(drifted_report, checks=("command_runs",), needle="checks")
    v.check("undeclared npm script flagged", undeclared is not None)

    score = drifted_report["score"]
    v.check("drifted fixture scores below 90", score < 90, f"drift score = {score}")
    v.check("drifted fixture has deterministic failures",
            drifted_report["scoring"]["deterministicFailures"] >= 4,
            f"deterministicFailures = {drifted_report['scoring']['deterministicFailures']}")

    # ---- 2. a clean repo is nearly clean --------------------------------------
    v.section("2. clean fixture")
    clean_score = clean_report["score"]
    v.check("clean fixture scores >= 95", clean_score >= 95, f"drift score = {clean_score}")
    v.check("clean fixture has zero deterministic failures",
            clean_report["scoring"]["deterministicFailures"] == 0,
            "findings: " + json.dumps(
                [{"title": f["title"], "file": f["ruleFile"], "line": f["ruleLine"],
                  "check": f["check"]} for f in clean_report["findings"]],
                default=str)[:400])

    # ---- 3. fix diffs apply ----------------------------------------------------
    v.section("3. fix diffs")
    diffs = [f for f in drifted_report["findings"] if f.get("diff")]
    v.check("at least one fix diff is produced", len(diffs) >= 1,
            f"{len(diffs)} finding(s) carry a diff")
    moved = find_finding(drifted_report, checks=("path_exists",), needle="Button.tsx")
    moved_diff = str((moved or {}).get("diff") or "")
    v.check("moved file is flagged with a rename diff to the real path",
            "src/ui/Button.tsx" in moved_diff and "src/components/Button.tsx" in moved_diff,
            "\n".join(moved_diff.splitlines()[:8]))
    for finding in diffs:
        v.check(f"git apply --check: {finding['check']} @ {finding['ruleFile']}",
                git_applies(drifted, finding["diff"]),
                "\n".join(finding["diff"].splitlines()[:6]))

    # ---- 4. evidence is real ---------------------------------------------------
    v.section("4. evidence")
    all_findings = drifted_report["findings"] + clean_report["findings"]
    reports: list[tuple[Path, dict[str, Any]]] = (
        [(drifted, f) for f in drifted_report["findings"]]
        + [(clean, f) for f in clean_report["findings"]]
    )
    bad_cites = [f"{repo.name}:{f['ruleFile']}:{f['ruleLine']}"
                 for repo, f in reports if not cited_line_is_real(repo, f)]
    v.check("every cited ruleFile:ruleLine is a real line", not bad_cites,
            "invalid: " + ", ".join(bad_cites[:6]))
    bad_evidence = [f"{f['id']}({f['check']})" for repo, f in reports
                    if not evidence_ok(repo, f)]
    v.check("every finding has non-empty evidence", not bad_evidence,
            "missing evidence: " + ", ".join(bad_evidence[:6]))
    empty = [f["id"] for f in all_findings if not str(f.get("reality", "")).strip()]
    v.check("every finding states the contradicting reality", not empty,
            "empty reality: " + ", ".join(empty[:6]))

    # ---- 5. nothing is invented ------------------------------------------------
    v.section("5. no fabrication")
    v.check("rules come from real harness files",
            all(r["source"] in DRIFTED or r["source"] in CLEAN
                for r in drifted_report["rules"]),
            f"sources: {sorted({r['source'] for r in drifted_report['rules']})}")
    v.check("deterministic-only run reports the LLM pass as skipped",
            any("no_api_key" in json.dumps(s) for s in drifted_report["skipped"]),
            f"skipped: {json.dumps(drifted_report['skipped'], default=str)[:220]}")
    unjudged = [f for f in drifted_report["findings"] if f.get("skipped")]
    v.check("unjudged conventions are surfaced, not hidden", bool(unjudged),
            f"{len(unjudged)} skipped finding(s)")
    calls = drifted_report["toolCalls"]
    v.check("every audit used the journaled readers",
            calls["total"] >= 3 and set(calls["byTool"]) >= {"read_harness", "inspect_repo"},
            f"tool calls: {json.dumps(calls['byTool'], default=str)[:240]}")
    v.check("audit.log records tool, args, duration and bytes", AUDIT_LOG.is_file()
            and _log_is_jsonl(AUDIT_LOG), f"log: {AUDIT_LOG}")

    # ---- summary ---------------------------------------------------------------
    print("\n" + "=" * 72)
    print(f"drifted fixture  score {score:>3}  findings {len(drifted_report['findings']):>2}  "
          f"rules {len(drifted_report['rules']):>2}  tool calls {calls['total']:>2}")
    print(f"clean fixture    score {clean_score:>3}  findings "
          f"{len(clean_report['findings']):>2}  "
          f"rules {len(clean_report['rules']):>2}")
    print(f"\n{v.passed} passed, {v.failed} failed")
    if v.failed:
        print("failed checks: " + ", ".join(v.failures))
        print("\nFAIL")
    else:
        print("PASS")

    findings_dump = workspace / "drifted-report.json"
    findings_dump.write_text(json.dumps(drifted_report, indent=2, default=str), encoding="utf-8")
    print(f"full drifted report: {findings_dump}")
    if "--keep" not in sys.argv:
        shutil.rmtree(workspace, ignore_errors=True)
    else:
        print(f"fixtures kept at {workspace}")
    return 0 if v.failed == 0 else 1


def _log_is_jsonl(path: Path) -> bool:
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        return False
    required = {"tool", "args", "durationMs", "bytesReturned"}
    for line in rows:
        try:
            record = json.loads(line)
        except ValueError:
            return False
        if not required.issubset(record):
            return False
    return True


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
