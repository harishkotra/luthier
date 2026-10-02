"""FastAPI server on :3001 -- runs the Luthier audit and streams progress.

Endpoints
    POST /api/audit            start a run: local path, https/git@ repo URL, or zip -> {id}
    GET  /api/audit/:id/stream SSE of per-stage progress (including the `fetch` stage)
    GET  /api/audit/:id        full result
    GET  /api/runs             recent runs
    GET  /api/evidence         re-read a cited file, 5 lines of context
    POST /api/inference/test   real probe of an OpenAI-compatible endpoint
    GET  /api/presets          provider presets (metadata only, never a key)
    GET  /api/scoring          the scoring constants the UI renders

Sources: a path is read in place; a URL is `git clone --depth 1` into `.luthier_sources/`
with `.git` removed and the directory deleted when the run ends; a dropped zip is extracted
under `.luthier_uploads/`. The clone is the product's only network access and it happens
before any check runs -- the five checks themselves are egress-free.

The audited repo is only ever read. When an inference endpoint is configured, a Strands
agent gets `read_harness` / `inspect_repo` and writes its own account of the audit; every
one of its tool calls lands in `audit.log` like any other.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import scoring
from .inference import (PRESETS, ChatConfig, env_config, load_dotenv, redacted_key,
                        test_connection)
from .rules import Auditor, _llm_usable
from .sources import (CLONE_TIMEOUT_SECONDS, MAX_CLONE_MB, RepoUrl, SourceError, clone_shallow,
                      parse_repo_url)
from .tools import ToolBundle, build_strands_tools

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
AUDIT_LOG = ROOT / "audit.log"
UPLOADS_DIR = ROOT / ".luthier_uploads"
SOURCES_DIR = ROOT / ".luthier_sources"
MAX_ZIP_BYTES = 200 * 1024 * 1024

log = logging.getLogger("luthier")

AGENT_SYSTEM_PROMPT = """You are Luthier, an auditor of AI-agent harnesses.

You have exactly two ways to learn about the repository: read_harness (file contents with
1-based line numbers) and inspect_repo (which files match a glob, their sizes, first 20
lines). Never state that a file exists, is empty, or contains something unless a tool
result in this conversation proves it. If a tool said it was truncated, say so.

After the deterministic checks run, summarise the drift in at most 6 sentences: the two or
three findings most likely to break an agent working in this repo, and the single cheapest
fix. Quote file:line for every claim. Do not invent findings, severity, or scores."""

TERMINAL = {"complete", "failed", "error"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def _env_repo_root(dotenv: dict[str, str]) -> str:
    return (os.environ.get("REPO_ROOT") or dotenv.get("REPO_ROOT") or "").strip()


app = FastAPI(title="Luthier", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

RUNS: dict[str, dict[str, Any]] = {}
SUBSCRIBERS: dict[str, list[asyncio.Queue]] = {}


# ------------------------------------------------------------------- helpers


def publish(run_id: str, event: dict[str, Any]) -> None:
    run = RUNS.get(run_id)
    if run is not None:
        run["events"].append(event)
        run["updatedAt"] = time.time()
    for queue in SUBSCRIBERS.get(run_id, []):
        queue.put_nowait(event)


def persist(run_id: str) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run = RUNS.get(run_id)
    if run is None:
        return
    target = RUNS_DIR / f"{run_id}.json"
    target.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")


def safe_extract_zip(upload: Path, destination: Path) -> Path:
    """Unzip with a traversal guard; returns the single top-level dir if there is one."""
    destination.mkdir(parents=True, exist_ok=True)
    resolved_dest = destination.resolve()
    with zipfile.ZipFile(upload) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != resolved_dest and resolved_dest not in target.parents:
                raise HTTPException(400, f"unsafe path in archive: {member.filename}")
            if member.is_dir():
                continue
            if member.filename.startswith("__MACOSX/") or Path(member.filename).name.startswith("._"):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
    entries = [p for p in destination.iterdir()]
    dirs = [p for p in entries if p.is_dir()]
    if len(dirs) == 1 and not [p for p in entries if p.is_file()]:
        return dirs[0]
    return destination


def resolve_chat_config(payload: dict[str, Any]) -> ChatConfig:
    dotenv = load_dotenv(ROOT / ".env")
    settings = payload.get("settings") or {}
    overrides = {
        "baseUrl": settings.get("baseUrl"),
        "apiKey": settings.get("apiKey"),
        "model": settings.get("model"),
        "temperature": settings.get("temperature"),
    }
    return env_config({k: v for k, v in overrides.items() if v not in (None, "")}, dotenv=dotenv)


# --------------------------------------------------------------------- audit


async def resolve_source(run_id: str, spec: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    """Turn a run's source spec into a directory on disk, cloning first if it is a URL.

    The fetch is the only network access in the product, and it happens here, before any
    check runs. From here on the auditor sees an ordinary local directory.
    """
    kind = spec.get("kind", "path")
    if kind == "url":
        source = parse_repo_url(str(spec.get("url", "")))
        if source is None:
            raise SourceError(f"not a repository URL: {spec.get('url')}")
        dest = SOURCES_DIR / run_id
        await asyncio.to_thread(_fetch_progress, run_id,
                                f"git clone --depth 1 {source.display}", "running")
        info = await asyncio.to_thread(
            clone_shallow, source, dest,
            timeout=float(_env_int("CLONE_TIMEOUT_SECONDS", int(CLONE_TIMEOUT_SECONDS))),
            max_mb=_env_int("MAX_CLONE_MB", MAX_CLONE_MB),
        )
        await asyncio.to_thread(_fetch_progress, run_id,
                                f"{info['bytes'] / 1e6:.1f} MB in {info['seconds']}s · "
                                f"commit {info['commit'] or 'unknown'} · .git removed", "done")
        return dest, {**info, "requested": str(spec.get("url"))}

    if kind == "zip":
        destination = Path(str(spec["cleanup"]))
        upload = destination / str(spec.get("uploadName", "upload.zip"))
        repo = await asyncio.to_thread(safe_extract_zip, upload, destination)
        return repo, {"kind": "zip", "file": str(spec.get("uploadName") or "upload.zip"),
                      "path": str(repo)}

    repo = Path(str(spec["path"]))
    return repo, {"kind": "path", "path": str(repo)}


def _fetch_progress(run_id: str, detail: str, status: str) -> None:
    publish(run_id, {"type": "progress", "stage": "fetch", "status": status, "detail": detail})


async def run_audit(run_id: str, spec: dict[str, Any], cfg: ChatConfig,
                    categories: set[str] | None) -> None:
    started = time.time()
    cleanup = Path(str(spec["cleanup"])) if spec.get("cleanup") else None
    try:
        repo, provenance = await resolve_source(run_id, spec)
        bundle = ToolBundle(repo_root=repo, audit_log_path=AUDIT_LOG, run_id=run_id,
                            max_bytes_per_file=_env_int("MAX_BYTES_PER_FILE", 20_000))

        async def emit(event: dict[str, Any]) -> None:
            publish(run_id, {"type": "progress", "runId": run_id, **event})

        auditor = Auditor(bundle, cfg=cfg, categories=categories)
        report = await auditor.run(emit=emit)
        publish(run_id, {"type": "status", "status": "scoring",
                         "detail": f"drift score {report['score']}"})
        narrative = await run_strands_loop(run_id, bundle, cfg, report, emit)
        report.update({
            "runId": run_id,
            "repoRequested": provenance.get("requested") or str(repo),
            "source": provenance,
            "startedAt": started,
            "finishedAt": time.time(),
            "durationSeconds": round(time.time() - started, 2),
            "inference": {
                "baseUrl": cfg.base_url,
                "model": cfg.model,
                "keyConfigured": cfg.key_present,
                "keyPreview": redacted_key(cfg.api_key),
                "used": _llm_usable(cfg),
            },
            "agentNarrative": narrative,
        })
        run = RUNS[run_id]
        run["report"] = report
        run["status"] = "complete"
        run["score"] = report["score"]
        run["findingCount"] = len(report["findings"])
        run["repo"] = str(repo)
        persist(run_id)
        publish(run_id, {"type": "done", "status": "complete", "score": report["score"],
                         "findings": len(report["findings"]),
                         "durationSeconds": report["durationSeconds"]})
    except Exception as exc:  # surfaced to the client as an error state
        run = RUNS.get(run_id, {})
        run["status"] = "failed"
        run["error"] = str(exc) if isinstance(exc, SourceError) else f"{type(exc).__name__}: {exc}"
        persist(run_id)
        publish(run_id, {"type": "error", "status": "failed", "detail": run["error"]})
        raise
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)


async def run_strands_loop(run_id: str, bundle: ToolBundle, cfg: ChatConfig,
                           report: dict[str, Any],
                           emit: Callable[[dict[str, Any]], Awaitable[None]]) -> dict[str, Any]:
    """Give the Strands agent the two readers and ask it to explain the drift."""
    if not _llm_usable(cfg):
        return {"status": "skipped: no_api_key", "text": None, "toolCalls": 0}
    top = sorted(report["findings"],
                 key=lambda f: (scoring.SEVERITIES.index(f["severity"])
                                if f["severity"] in scoring.SEVERITIES else 9,))[:12]
    prompt = json.dumps({
        "repo": report["repo"],
        "score": report["score"],
        "counts": report["scoring"]["counts"],
        "skipped": report["skipped"],
        "topFindings": [{"category": f["category"], "severity": f["severity"],
                         "check": f["check"], "title": f["title"], "rule": f["ruleFile"],
                         "line": f["ruleLine"], "reality": f["reality"]} for f in top],
        "instruction": "Verify the two highest-severity findings with your tools, then summarise.",
    }, indent=2, default=str)
    try:
        from strands import Agent
        from strands.models.openai import OpenAIModel

        model = OpenAIModel(
            client_args={"base_url": cfg.base_url, "api_key": cfg.api_key},
            model_id=cfg.model,
            params={"temperature": cfg.temperature, "max_tokens": 900},
            stream=False,
        )
        agent = Agent(model=model, tools=build_strands_tools(bundle),
                      system_prompt=AGENT_SYSTEM_PROMPT)

        def call() -> Any:
            return agent(prompt)

        before = len(bundle.journal)
        await emit({"stage": "agent-loop", "status": "running",
                    "detail": "Strands agent verifying with read_harness / inspect_repo"})
        result = await asyncio.wait_for(asyncio.to_thread(call), timeout=180)
        text = _result_text(result)
        calls = len(bundle.journal) - before
        await emit({"stage": "agent-loop", "status": "done",
                    "detail": f"{calls} tool call(s) by the agent"})
        return {"status": "ok", "text": text[:4_000], "toolCalls": calls}
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        await emit({"stage": "agent-loop", "status": "skipped", "detail": clip_text(detail)})
        return {"status": f"skipped: {clip_text(detail)}", "text": None, "toolCalls": 0}


def _result_text(result: Any) -> str:
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        blocks = message.get("content") or []
        if isinstance(blocks, str):
            return blocks
        return "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
    return str(result or "")


def clip_text(value: str, limit: int = 160) -> str:
    flat = " ".join(str(value or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# -------------------------------------------------------------------- routes


@app.get("/api/health")
async def health() -> dict[str, Any]:
    dotenv = load_dotenv(ROOT / ".env")
    cfg = env_config({}, dotenv=dotenv)
    return {
        "ok": True,
        "service": "luthier",
        "version": app.version,
        "repoRootDefault": _env_repo_root(dotenv) or None,
        "inferenceConfigured": _llm_usable(cfg),
        "baseUrl": cfg.base_url,
        "model": cfg.model,
        "keyPreview": redacted_key(cfg.api_key),
        "auditLog": str(AUDIT_LOG),
        "maxBytesPerFile": _env_int("MAX_BYTES_PER_FILE", 20_000),
        "git": shutil.which("git") is not None,
        "cloneTimeoutSeconds": _env_int("CLONE_TIMEOUT_SECONDS", int(CLONE_TIMEOUT_SECONDS)),
        "cloneMaxMb": _env_int("MAX_CLONE_MB", MAX_CLONE_MB),
    }


@app.get("/api/presets")
async def presets() -> dict[str, Any]:
    dotenv = load_dotenv(ROOT / ".env")
    resolved = {preset.get("keyEnv"): redacted_key(dotenv.get(str(preset.get("keyEnv")), ""))
                for preset in PRESETS}
    return {"presets": PRESETS, "envKeysConfigured": resolved,
            "repoRootDefault": _env_repo_root(dotenv) or None}


@app.get("/api/scoring")
async def scoring_constants() -> dict[str, Any]:
    return scoring.scoring_constants()


@app.post("/api/inference/test")
async def inference_test(payload: dict[str, Any]) -> JSONResponse:
    cfg = resolve_chat_config(payload or {})
    result = await test_connection(cfg)
    result["keyPreview"] = redacted_key(cfg.api_key)
    result["keyConfigured"] = cfg.key_present
    return JSONResponse(result)


@app.post("/api/audit")
async def start_audit(request: Request, file: UploadFile | None = File(None),
                      repoPath: str | None = Form(None),
                      settings: str | None = Form(None),
                      categories: str | None = Form(None)) -> JSONResponse:
    payload: dict[str, Any] = {}
    if not file:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            payload = {}
    if settings:
        payload["settings"] = json.loads(settings)
    if categories:
        payload["categories"] = json.loads(categories)
    if repoPath:
        payload["repoPath"] = repoPath

    run_id = uuid.uuid4().hex[:12]
    cfg = resolve_chat_config(payload)
    raw = str(payload.get("repoPath") or cfg_model_default(payload) or "").strip()
    if not raw and file is None:
        raw = _env_repo_root(load_dotenv(ROOT / ".env"))
    if not raw and file is None:
        raise HTTPException(400, "repoPath is required (a local path or an https/git@ repo URL)")

    spec: dict[str, Any]
    requested = raw
    if file is not None:
        destination = UPLOADS_DIR / run_id
        destination.mkdir(parents=True, exist_ok=True)
        upload_name = Path(file.filename or "upload.zip").name
        upload_path = destination / upload_name
        async with upload_path.open("wb") as sink:
            while chunk := await file.read(1 << 20):
                sink.write(chunk)
                if upload_path.stat().st_size > MAX_ZIP_BYTES:
                    shutil.rmtree(destination, ignore_errors=True)
                    raise HTTPException(413, "archive too large")
        spec = {"kind": "zip", "cleanup": str(destination), "uploadName": upload_name}
        requested = upload_name
    else:
        try:
            remote = parse_repo_url(raw)
        except SourceError as exc:
            raise HTTPException(400, str(exc)) from exc
        if remote is not None:
            spec = {"kind": "url", "url": raw, "cleanup": str(SOURCES_DIR / run_id)}
            requested = remote.display
        else:
            repo = Path(raw).expanduser()
            if not repo.is_absolute():
                repo = (ROOT / repo).resolve()
            if not repo.is_dir():
                raise HTTPException(404, f"no such directory: {raw}")
            spec = {"kind": "path", "path": str(repo)}
            requested = str(repo)

    categories_payload = payload.get("categories")
    selected = set(categories_payload) if isinstance(categories_payload, list) and categories_payload\
        else None

    RUNS[run_id] = {
        "id": run_id, "status": "running", "repo": requested, "source": spec["kind"],
        "requested": requested,
        "startedAt": time.time(), "updatedAt": time.time(), "events": [],
        "report": None, "score": None, "findingCount": 0,
        "inference": {"baseUrl": cfg.base_url, "model": cfg.model,
                      "keyConfigured": cfg.key_present, "keyPreview": redacted_key(cfg.api_key)},
        "categories": sorted(selected) if selected else "all",
    }
    persist(run_id)
    task = asyncio.create_task(run_audit(run_id, spec, cfg, selected))
    task.add_done_callback(lambda finished: _on_audit_finished(run_id, finished))
    return JSONResponse({"id": run_id, "status": "running", "repo": requested,
                         "source": spec["kind"],
                         "inference": RUNS[run_id]["inference"]}, status_code=202)


def cfg_model_default(payload: dict[str, Any]) -> str:
    settings = payload.get("settings") or {}
    return str(settings.get("repoRoot") or "")


def _on_audit_finished(run_id: str, task: asyncio.Task) -> None:
    """Consume the task exception so it is never lost; run_audit already reported it."""
    if task.cancelled():
        publish(run_id, {"type": "error", "status": "failed", "detail": "cancelled"})
    elif error := task.exception():
        log.warning("run %s failed: %s", run_id, error)
    for queue in SUBSCRIBERS.get(run_id, []):
        queue.put_nowait(None)


@app.get("/api/audit/{run_id}/stream")
async def stream(run_id: str, request: Request) -> StreamingResponse:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, f"unknown run: {run_id}")
    queue: asyncio.Queue = asyncio.Queue()
    for event in run["events"]:
        queue.put_nowait(event)
    if run.get("status") in TERMINAL:
        queue.put_nowait(None)
    SUBSCRIBERS.setdefault(run_id, []).append(queue)

    async def pump() -> Any:
        try:
            yield f"data: {json.dumps({'type': 'hello', 'runId': run_id, 'status': run['status']})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event.get("type") in {"done", "error"}:
                    break
        finally:
            SUBSCRIBERS.get(run_id, []).remove(queue)

    return StreamingResponse(pump(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache", "x-accel-buffering": "no"})


@app.get("/api/audit/{run_id}")
async def result(run_id: str) -> dict[str, Any]:
    run = RUNS.get(run_id)
    if run is not None and run.get("report"):
        return run["report"]
    disk = RUNS_DIR / f"{run_id}.json"
    if disk.is_file():
        stored = json.loads(disk.read_text(encoding="utf-8"))
        if stored.get("report"):
            return stored["report"]
        return stored
    raise HTTPException(404, f"unknown run: {run_id}")


@app.get("/api/runs")
async def runs(limit: int = 30) -> dict[str, Any]:
    seen = {run["id"]: run for run in RUNS.values()}
    if RUNS_DIR.is_dir():
        for path in sorted(RUNS_DIR.glob("*.json"), reverse=True)[:limit]:
            if path.stem in seen:
                continue
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            seen[path.stem] = stored
    rows = sorted(seen.values(), key=lambda r: r.get("startedAt") or 0, reverse=True)[:limit]
    return {"runs": [{
        "id": r.get("id"), "status": r.get("status"), "repo": r.get("repo"),
        "score": r.get("score"), "findingCount": r.get("findingCount"),
        "startedAt": r.get("startedAt"), "error": r.get("error"),
    } for r in rows]}


@app.get("/api/evidence")
async def evidence(path: str, line: int = 1, repo: str = "", ctx: int = 5) -> dict[str, Any]:
    """Re-read a cited file from disk so the Evidence panel never trusts stale text."""
    root = Path(repo).expanduser() if repo else None
    if root is None or not root.is_dir():
        default = _env_repo_root(load_dotenv(ROOT / ".env"))
        if not default:
            raise HTTPException(400, "repo query parameter is required")
        root = Path(default)
    bundle = ToolBundle(repo_root=root, audit_log_path=AUDIT_LOG, run_id="evidence",
                        max_bytes_per_file=_env_int("MAX_BYTES_PER_FILE", 20_000))
    payload = bundle.read_lines(path, line, context=max(0, min(ctx, 50)))
    if not payload.get("exists"):
        raise HTTPException(404, f"no such file: {path}")
    return payload


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail, "status": exc.status_code},
                        status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("agent.main:app", host="127.0.0.1",
                port=_env_int("PORT", 3001), reload=False)
