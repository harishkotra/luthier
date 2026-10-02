"""Drift scoring.

Every weight and cap lives here as a constant and is served to the UI through
`scoring_constants()` so a score is never a black box.
"""

from __future__ import annotations

from typing import Any

BASE_SCORE = 100
FLOOR_SCORE = 0

# Penalty per finding, by what kind of check produced it.
PENALTY_DETERMINISTIC_FAILURE = 8   # a rule that provably no longer matches the repo
PENALTY_SEMANTIC_VIOLATION = 5      # a convention judged violated against real code
PENALTY_SEMANTIC_UNKNOWN = 1        # a convention nobody can confirm either way

# A category cannot sink the score on its own: 4 deterministic failures saturate it.
MAX_PENALTY_PER_CATEGORY = 32

# Findings per check beyond this are folded into one "and N more" row, so a 500-path
# repo cannot produce 500 rows. The cap is per check, per direction.
MAX_FINDINGS_PER_CHECK = 40

SEVERITIES = ("critical", "high", "medium", "low", "info")

CATEGORIES = ("paths", "commands", "skills", "mcp", "conventions")

CATEGORY_LABELS = {
    "paths": "Stale paths",
    "commands": "Commands",
    "skills": "Skills",
    "mcp": "MCP servers",
    "conventions": "Conventions",
}

# Human name for each check type, rendered in the findings table.
CHECK_LABELS = {
    "path_exists": "path_exists",
    "command_runs": "command_runs",
    "file_referenced": "file_referenced",
    "server_reachable": "server_reachable",
    "semantic": "semantic",
    "convention_static": "convention (mechanical)",
    "convention_llm": "convention (LLM)",
    "skill_unlisted": "file_referenced (undeclared skill)",
    "mcp_schema": "server_reachable (schema)",
    "mcp_undocumented": "server_reachable (undocumented)",
    "harness_missing": "harness coverage",
    "harness_empty": "harness coverage",
}


def finding_penalty(finding: dict[str, Any]) -> int:
    if finding.get("skipped") or finding.get("advisory"):
        return 0
    verdict = finding.get("verdict")
    if verdict == "unknown":
        return PENALTY_SEMANTIC_UNKNOWN
    if not finding.get("deterministic", True):
        return PENALTY_SEMANTIC_VIOLATION
    return PENALTY_DETERMINISTIC_FAILURE


def scoring_constants() -> dict[str, Any]:
    """The exact table the UI renders under "how this score was computed"."""
    return {
        "baseScore": BASE_SCORE,
        "floorScore": FLOOR_SCORE,
        "penalties": {
            "deterministicFailure": PENALTY_DETERMINISTIC_FAILURE,
            "semanticViolation": PENALTY_SEMANTIC_VIOLATION,
            "semanticUnknown": PENALTY_SEMANTIC_UNKNOWN,
        },
        "maxPenaltyPerCategory": MAX_PENALTY_PER_CATEGORY,
        "maxFindingsPerCheck": MAX_FINDINGS_PER_CHECK,
        "advisoryPenalty": 0,
        "categories": list(CATEGORIES),
        "categoryLabels": CATEGORY_LABELS,
        "severities": list(SEVERITIES),
        "formula": (
            f"score = max({FLOOR_SCORE}, {BASE_SCORE} - sum(min(category_penalty, "
            f"{MAX_PENALTY_PER_CATEGORY}))) where each finding costs "
            f"{PENALTY_DETERMINISTIC_FAILURE} (deterministic) / "
            f"{PENALTY_SEMANTIC_VIOLATION} (semantic violation) / "
            f"{PENALTY_SEMANTIC_UNKNOWN} (unknown)"
        ),
    }


def compute_score(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the drift score plus the per-category ledger that produced it."""
    per_category: dict[str, dict[str, Any]] = {
        category: {
            "label": CATEGORY_LABELS.get(category, category),
            "count": 0,
            "rawPenalty": 0,
            "appliedPenalty": 0,
            "capped": False,
            "bySeverity": {severity: 0 for severity in SEVERITIES},
            "deterministic": 0,
            "semantic": 0,
            "unknown": 0,
            "advisory": 0,
        }
        for category in CATEGORIES
    }
    breakdown: list[dict[str, Any]] = []
    skipped = 0
    advisory = 0

    for finding in findings:
        category = finding.get("category") or "conventions"
        bucket = per_category.setdefault(category, {
            "label": CATEGORY_LABELS.get(category, category),
            "count": 0,
            "rawPenalty": 0,
            "appliedPenalty": 0,
            "capped": False,
            "bySeverity": {severity: 0 for severity in SEVERITIES},
            "deterministic": 0,
            "semantic": 0,
            "unknown": 0,
            "advisory": 0,
        })
        if finding.get("skipped"):
            skipped += 1
            continue
        penalty = finding_penalty(finding)
        bucket["count"] += 1
        bucket["rawPenalty"] += penalty
        severity = finding.get("severity", "info")
        bucket["bySeverity"][severity] = bucket["bySeverity"].get(severity, 0) + 1
        verdict = finding.get("verdict")
        if finding.get("advisory"):
            advisory += 1
            bucket["advisory"] += 1
        elif verdict == "unknown":
            bucket["unknown"] += 1
        elif finding.get("deterministic", True):
            bucket["deterministic"] += 1
        else:
            bucket["semantic"] += 1
        breakdown.append({
            "findingId": finding.get("id"),
            "category": category,
            "kind": ("advisory" if finding.get("advisory")
                     else "unknown" if verdict == "unknown"
                     else "deterministic" if finding.get("deterministic", True)
                     else "semantic"),
            "penalty": penalty,
        })

    total_raw = 0
    total_applied = 0
    for bucket in per_category.values():
        applied = min(bucket["rawPenalty"], MAX_PENALTY_PER_CATEGORY)
        bucket["capped"] = bucket["rawPenalty"] > applied
        bucket["appliedPenalty"] = applied
        total_raw += bucket["rawPenalty"]
        total_applied += applied

    score = max(FLOOR_SCORE, BASE_SCORE - total_applied)
    deterministic_failures = sum(1 for f in findings
                                 if f.get("deterministic") and not f.get("skipped")
                                 and not f.get("advisory") and f.get("verdict") != "unknown")
    return {
        "score": score,
        "base": BASE_SCORE,
        "totalRawPenalty": total_raw,
        "totalAppliedPenalty": total_applied,
        "perCategory": per_category,
        "counts": {category: bucket["count"] for category, bucket in per_category.items()},
        "deterministicFailures": deterministic_failures,
        "semanticViolations": sum(b["semantic"] for b in per_category.values()),
        "unknowns": sum(b["unknown"] for b in per_category.values()),
        "skipped": skipped,
        "breakdown": breakdown,
        "constants": scoring_constants(),
    }
