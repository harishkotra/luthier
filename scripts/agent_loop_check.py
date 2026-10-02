#!/usr/bin/env python
"""Prove the Strands audit loop works end to end, without a hosted key.

Boots a minimal OpenAI-compatible endpoint on 127.0.0.1:3002 that always asks for one
`inspect_repo` tool call and then answers in text, points the running Luthier server at it,
runs a real audit, and asserts:

  * the agent's tool call went through ToolBundle and was written to audit.log,
  * the run's `agentNarrative.status` is "ok" with text and a tool-call count,
  * the deterministic findings are unchanged by the model being present.

This tests our integration with Strands, not any provider's quality: the mock is ours.
Run `npm run dev` (or uvicorn agent.main:app) first, then `python scripts/agent_loop_check.py`.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MOCK_PORT = 3002
APP_PORT = 3001
APP = f"http://127.0.0.1:{APP_PORT}"
REPO = str(Path(__file__).resolve().parent.parent / "tmp" / "demo-repo")

REQUESTS: list[dict] = []


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return None

    def _send(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.startswith("/v1/models"):
            self._send({"object": "list",
                        "data": [{"id": "mock-auditor", "object": "model"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        REQUESTS.append(body)
        messages = body.get("messages", [])
        tool_answers = [m for m in messages if m.get("role") == "tool"]
        if not tool_answers:
            call = {
                "id": "call_inspect_1",
                "type": "function",
                "function": {
                    "name": "inspect_repo",
                    "arguments": json.dumps({"globs": ["CLAUDE.md", "src/*.ts"]}),
                },
            }
            self._send({
                "id": "mock-1", "object": "chat.completion", "created": int(time.time()),
                "model": body.get("model", "mock-auditor"),
                "choices": [{"index": 0, "finish_reason": "tool_calls",
                             "message": {"role": "assistant", "content": None,
                                         "tool_calls": [call]}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
            })
            return
        self._send({
            "id": "mock-2", "object": "chat.completion", "created": int(time.time()),
            "model": body.get("model", "mock-auditor"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": ("Verified: CLAUDE.md exists and src/*.ts matched "
                                                 "2 files. The cheapest fix is repointing the "
                                                 "`notes` MCP entry at an installed binary.")}}],
            "usage": {"prompt_tokens": 90, "completion_tokens": 30, "total_tokens": 120},
        })


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read())


def main() -> int:
    if not port_open("127.0.0.1", APP_PORT):
        print(f"FAIL  Luthier server is not running on {APP} -- start it with `npm run dev`")
        return 1
    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Mock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    print(f"mock OpenAI-compatible endpoint on 127.0.0.1:{MOCK_PORT}")

    before = log_lines()
    run = post_json(f"{APP}/api/audit", {
        "repoPath": REPO,
        "settings": {"baseUrl": f"http://127.0.0.1:{MOCK_PORT}/v1",
                      "apiKey": "mock-provider-key", "model": "mock-auditor"},
    })
    run_id = run["id"]
    print(f"run {run_id}: waiting for the audit to finish")
    report = None
    for _ in range(120):
        time.sleep(1)
        status = get_json(f"{APP}/api/runs")
        row = next((r for r in status["runs"] if r["id"] == run_id), None)
        if row and row["status"] in {"complete", "failed"}:
            report = get_json(f"{APP}/api/audit/{run_id}")
            break
    if report is None:
        print("FAIL  audit did not finish within 120s")
        return 1

    narrative = report.get("agentNarrative") or {}
    checks = [
        ("mock endpoint received the agent's request", len(REQUESTS) >= 1),
        ("agent loop reported ok", narrative.get("status") == "ok"),
        ("agent made at least one journaled tool call", int(narrative.get("toolCalls") or 0) >= 1),
        ("narrative text came back", bool(str(narrative.get("text") or "").strip())),
        ("deterministic findings still present", len(report["findings"]) >= 5),
        ("drift score computed", isinstance(report.get("score"), int) and report["score"] < 90),
        ("agent tool call written to audit.log", log_lines() - before >= 1
         and "inspect_repo" in tail_log(before)),
        ("API key never echoed to the client",
         "mock-provider-key" not in json.dumps(report)),
    ]
    print("\nStrands audit-loop check")
    failures = 0
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        failures += 0 if ok else 1
    print(f"\n  narrative: {str(narrative.get('text'))[:180]}")
    print(f"  provider turns: {len(REQUESTS)}, agent tool calls: {narrative.get('toolCalls')}")
    print(f"  score: {report['score']}, findings: {len(report['findings'])}")
    print("\n" + ("PASS" if failures == 0 else "FAIL"))
    mock.shutdown()
    return 0 if failures == 0 else 1


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def log_lines() -> int:
    path = Path(__file__).resolve().parent.parent / "audit.log"
    return sum(1 for _ in path.open()) if path.is_file() else 0


def tail_log(before: int) -> str:
    path = Path(__file__).resolve().parent.parent / "audit.log"
    lines = path.read_text().splitlines()[before:]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
