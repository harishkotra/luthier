"""Git-URL sources.

This is the only module in Luthier that may touch the network, it does so solely when the
user submits a URL, and it never touches the audited repo of anyone but itself: clones land
in `.luthier_sources/<runId>/` and are deleted when the run finishes.

The five drift checks stay egress-free; they only ever see a directory on disk.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

CLONE_TIMEOUT_SECONDS = 180
MAX_CLONE_MB = 400
MAX_GIT_OUTPUT_CHARS = 2000

# `git@host:owner/repo.git` and `https://host/owner/repo[/tree/ref]`
SSH_URL_RE = re.compile(r"^(?P<user>[\w.-]+)@(?P<host>[\w.-]+):(?P<path>[\w./~@%+-]+?)(?:\.git)?$")
HTTPS_URL_RE = re.compile(
    r"^(?P<scheme>https?)://(?P<host>[\w.-]+)(?::(?P<port>\d+))?/(?P<path>[\w.~@%+-]+(?:/[\w.~@%+-]+)*)(?:\.git)?/?$"
)
CREDENTIALS_RE = re.compile(r"^[a-z]+://[^/@]*@", re.I)
# `owner/repo/tree/<ref>/path` (GitHub) and `group/…/repo/-/tree/<ref>/path` (GitLab)
TREE_REF_RE = re.compile(
    r"^(?P<repo>[\w.~@%+-]+(?:/[\w.~@%+-]+)+?)(?:/-)?/(?:tree|blob|raw)/(?P<ref>[^/]+)(?:/|$)"
)
HOSTS_NEEDING_GIT_SUFFIX = {"github.com", "gitlab.com", "bitbucket.org"}


class SourceError(Exception):
    """A clone that could not be done, or should not be attempted."""


@dataclass
class RepoUrl:
    clone_url: str
    ref: str | None = None
    display: str = ""

    @property
    def host(self) -> str:
        match = re.search(r"://[^/]*@?([\w.-]+)", self.clone_url) or re.search(r"@([\w.-]+):", self.clone_url)
        return match.group(1) if match else ""


def parse_repo_url(raw: str) -> RepoUrl | None:
    """Recognise a repo URL, or None when the input is a plain filesystem path.

    Deliberately strict: schemes other than https/ssh, embedded credentials, and single-
    segment paths are refused, because each is a way for a pasted string to become
    something other than a public clone.
    """
    text = (raw or "").strip().strip('"').strip("'")
    if not text or text.startswith(("/", "./", "../", "~", ".")):
        return None
    if "://" not in text and "@" not in text:
        return None
    if CREDENTIALS_RE.match(text):
        raise SourceError("credentials embedded in a URL are not accepted; use your git "
                          "credential helper or an SSH remote instead")

    if text.startswith(("http://", "https://")):
        match = HTTPS_URL_RE.match(text)
        if not match:
            raise SourceError(f"not a cloneable repository URL: {text}")
        path = match.group("path")
        ref_match = TREE_REF_RE.match(path)
        ref = ref_match.group("ref") if ref_match else None
        if ref_match:
            path = ref_match.group("repo")
        if "/" not in path:
            raise SourceError("a repository URL needs an owner and a repo name")
        port = f":{match.group('port')}" if match.group("port") else ""
        base = f"{match.group('scheme')}://{match.group('host')}{port}/{path}"
        clone_url = base if base.endswith(".git") or match.group("host") not in HOSTS_NEEDING_GIT_SUFFIX \
            else f"{base}.git"
        return RepoUrl(clone_url=clone_url, ref=ref, display=base)

    match = SSH_URL_RE.match(text)
    if match:
        path = match.group("path")
        if "/" not in path:
            raise SourceError("a repository URL needs an owner and a repo name")
        clone_url = f"{match.group('user')}@{match.group('host')}:{path}.git"
        return RepoUrl(clone_url=clone_url, display=f"{match.group('host')}/{path}")

    raise SourceError(f"unsupported source: {text} (https:// or git@ remotes only)")


def directory_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"      # never block waiting for a password prompt
    env["GIT_LFS_SKIP_SMUDGE"] = "1"      # do not pull LFS object bodies into a read-only audit
    env.setdefault("GIT_ASKPASS", "echo")
    return env


def _run_git(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout,
                              env=_git_env())
    except FileNotFoundError as exc:
        raise SourceError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceError(f"git clone timed out after {timeout:.0f}s") from exc


def _clip(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= MAX_GIT_OUTPUT_CHARS else flat[:MAX_GIT_OUTPUT_CHARS - 1] + "…"


def clone_shallow(source: RepoUrl, dest: Path, *, timeout: float = CLONE_TIMEOUT_SECONDS,
                  max_mb: int = MAX_CLONE_MB) -> dict[str, object]:
    """`git clone --depth 1` into dest, then drop .git so nothing can look at history.

    Returns a small provenance record: url, ref, commit, size, timing.
    """
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def attempt(with_ref: bool) -> subprocess.CompletedProcess[str]:
        args = ["clone", "--depth", "1", "--single-branch", "--no-tags"]
        if with_ref and source.ref:
            args += ["--branch", source.ref]
        args += ["--", source.clone_url, str(dest)]
        return _run_git(args, timeout)

    started = time.perf_counter()
    result = attempt(True)
    ref_applied = bool(source.ref)
    if result.returncode != 0:
        if source.ref:
            shutil.rmtree(dest, ignore_errors=True)
            result = attempt(False)
            ref_applied = False
        if result.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            detail = _clip(result.stderr or result.stdout)
            raise SourceError(f"git clone failed: {detail or 'no output from git'}")

    commit: str | None = None
    described = _run_git(["-C", str(dest), "rev-parse", "--short", "HEAD"], timeout=30)
    if described.returncode == 0:
        commit = described.stdout.strip()

    size = directory_bytes(dest)
    limit = max_mb * 1024 * 1024
    if size > limit:
        shutil.rmtree(dest, ignore_errors=True)
        raise SourceError(f"clone is {size / 1e6:.0f} MB, over the {max_mb} MB cap "
                          f"(set MAX_CLONE_MB to raise it)")

    shutil.rmtree(dest / ".git", ignore_errors=True)

    return {
        "kind": "url",
        "url": source.display or source.clone_url,
        "ref": source.ref if ref_applied else None,
        "refRequested": source.ref,
        "commit": commit,
        "bytes": size,
        "seconds": round(time.perf_counter() - started, 2),
        "path": str(dest),
    }
