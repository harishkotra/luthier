"""Plain HTTP chat calls to any OpenAI-compatible endpoint.

Deliberately SDK-free: one POST to `{baseUrl}/chat/completions` with
`response_format: {"type": "json_object"}`, so a provider that chokes on the strict-JSON
flag can be retried without it and the failure surfaced honestly. API keys stay server-side.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.particle.ai/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"
REQUEST_TIMEOUT = float(os.environ.get("LUTHIER_LLM_TIMEOUT", "120"))
MAX_BODY_ECHO = 4000

# Surfaced to the Settings panel through /api/presets -- metadata only, never a key.
PRESETS: list[dict[str, Any]] = [
    {
        "id": "particle",
        "label": "Particle.ai",
        "default": True,
        "baseUrl": "https://api.particle.ai/v1",
        "model": "deepseek-v4.1-flash",
        "needsKey": True,
        "keyEnv": "OPENAI_API_KEY",
    },
    {
        "id": "lmstudio",
        "label": "LM Studio",
        "baseUrl": "http://localhost:1234/v1",
        "model": "local-model",
        "needsKey": False,
        "keyEnv": "LMSTUDIO_API_KEY",
        "baseUrlEnv": "LMSTUDIO_BASE_URL",
    },
    {
        "id": "ollama",
        "label": "Ollama",
        "baseUrl": "http://localhost:11434/v1",
        "model": "llama3.2",
        "needsKey": False,
        "keyEnv": "OLLAMA_API_KEY",
        "baseUrlEnv": "OLLAMA_BASE_URL",
    },
    {
        "id": "gemini",
        "label": "Gemini",
        "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.5-flash",
        "needsKey": True,
        "keyEnv": "GEMINI_API_KEY",
        "baseUrlEnv": "GEMINI_BASE_URL",
    },
    {"id": "custom", "label": "Custom", "baseUrl": "", "model": "", "needsKey": False},
]


PLACEHOLDER_KEYS = {"pa-xxxxxxxx", "changeme", "change-me", "your-key", "sk-xxx", "none", ""}


def is_placeholder_key(value: str) -> bool:
    lowered = (value or "").strip().lower()
    if lowered in PLACEHOLDER_KEYS:
        return True
    return bool(re.fullmatch(r"(pa|sk|key)[-_]?x{4,}", lowered))


@dataclass
class ChatConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    timeout: float = REQUEST_TIMEOUT
    source: str = "env"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())

    @property
    def key_present(self) -> bool:
        return bool(self.api_key.strip()) and not is_placeholder_key(self.api_key)


class NoApiKey(Exception):
    """Raised when a judgement needs the LLM but no key is configured."""


def normalize_base_url(raw: str) -> str:
    url = (raw or "").strip().rstrip("/")
    if url and not re.match(r"^https?://", url):
        url = "https://" + url
    return url


def load_dotenv(path: Path | str = ".env") -> dict[str, str]:
    """Minimal .env reader -- no dependency, ignores comments and blank lines."""
    file = Path(path)
    if not file.is_file():
        return {}
    out: dict[str, str] = {}
    for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip()
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def env_config(overrides: dict[str, Any] | None = None, dotenv: dict[str, str] | None = None) -> ChatConfig:
    """Build a ChatConfig from .env + process env, then apply request overrides on top."""
    cfg = ChatConfig(source="env")
    env = dotenv if dotenv is not None else load_dotenv()

    def pick(key: str, default: str = "") -> str:
        return os.environ.get(key) or env.get(key) or default

    cfg.base_url = normalize_base_url(pick("OPENAI_BASE_URL", DEFAULT_BASE_URL))
    cfg.api_key = pick("OPENAI_API_KEY")
    cfg.model = pick("MODEL", DEFAULT_MODEL)

    for key, value in (overrides or {}).items():
        if value in (None, ""):
            continue
        if key == "baseUrl":
            cfg.base_url = normalize_base_url(str(value))
            cfg.source = "request"
        elif key == "apiKey":
            cfg.api_key = str(value)
            cfg.source = "request"
        elif key == "model":
            cfg.model = str(value)
        elif key == "temperature":
            try:
                cfg.temperature = float(value)
            except (TypeError, ValueError):
                pass
        elif key == "timeout":
            try:
                cfg.timeout = min(float(value), 600.0)
            except (TypeError, ValueError):
                pass
    return cfg


def redacted_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:3]}…{key[-4:]}"


# --------------------------------------------------------------------- chat


@dataclass
class ChatResult:
    ok: bool
    status: int | None = None
    content: str = ""
    data: Any = None
    error: str | None = None
    body: str = ""
    latency_ms: int = 0
    notes: list[str] = field(default_factory=list)
    model: str = ""
    base_url: str = ""
    usage: dict[str, Any] | None = None

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "content": self.content,
            "data": self.data,
            "error": self.error,
            "body": self.body,
            "latencyMs": self.latency_ms,
            "notes": self.notes,
            "model": self.model,
            "baseUrl": self.base_url,
        }


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_object(text: str) -> Any:
    """Pull the first JSON object out of a reply, tolerating code fences and prose."""
    if not text:
        raise ValueError("empty completion")
    candidates = [text.strip()]
    candidates += [m.strip() for m in _JSON_FENCE_RE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    raise ValueError(f"reply was not JSON: {last_error}")


def _endpoint(base_url: str) -> str:
    base = normalize_base_url(base_url)
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


def _headers(api_key: str) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    return headers


def _extract_payload(body: dict[str, Any]) -> tuple[str, str]:
    """Return (text, finish_reason) from a chat completion, across provider quirks."""
    choices = body.get("choices") or []
    if not choices:
        if "error" in body:
            err = body["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ValueError(f"provider error: {msg}")
        raise ValueError(f"provider returned no choices: {json.dumps(body)[:300]}")
    message = choices[0].get("message") or {}
    text = message.get("content")
    if text in (None, ""):
        # Some reasoning models park the answer in alternate fields.
        text = message.get("reasoning_content") or message.get("reasoning") or ""
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    if not text and message.get("tool_calls"):
        text = ""
    return str(text), str(choices[0].get("finish_reason") or "")


async def chat(cfg: ChatConfig, messages: list[dict[str, Any]], *, json_mode: bool = True) -> ChatResult:
    """POST /chat/completions. On JSON mode, retry once without `response_format`."""
    if not cfg.configured:
        return ChatResult(ok=False, error="base URL and model must be configured", model=cfg.model,
                          base_url=cfg.base_url)
    if json_mode and not cfg.key_present and "localhost" not in cfg.base_url and "127.0.0.1" not in cfg.base_url:
        raise NoApiKey("no API key configured")

    notes: list[str] = []
    result = ChatResult(ok=False, model=cfg.model, base_url=cfg.base_url)
    attempts: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
    }
    attempts.append({**payload, "response_format": {"type": "json_object"}} if json_mode else payload)
    if json_mode:
        attempts.append(payload)

    url = _endpoint(cfg.base_url)
    started = time.perf_counter()
    last_error = ""
    for index, body in enumerate(attempts):
        try:
            async with httpx.AsyncClient(timeout=cfg.timeout, follow_redirects=True) as client:
                response = await client.post(url, headers=_headers(cfg.api_key), json=body)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if index == 0 and json_mode:
                notes.append("endpoint unreachable; retried without response_format")
            result.error = last_error
            continue
        result.status = response.status_code
        result.latency_ms = round((time.perf_counter() - started) * 1000)
        raw = response.text
        result.body = raw[:MAX_BODY_ECHO]
        if response.status_code >= 400:
            last_error = f"HTTP {response.status_code}"
            result.error = f"{last_error}: {raw[:600]}"
            continue
        try:
            parsed = response.json()
        except ValueError:
            result.error = f"response was not JSON: {raw[:400]}"
            last_error = "non-JSON response"
            continue
        try:
            text, finish = _extract_payload(parsed)
        except ValueError as exc:
            result.error = str(exc)
            last_error = str(exc)
            continue
        result.content = text
        result.model = str(parsed.get("model") or cfg.model)
        result.usage = parsed.get("usage")  # type: ignore[attr-defined]
        if json_mode:
            try:
                result.data = parse_json_object(text)
            except ValueError as exc:
                result.error = f"{exc}; raw reply follows" if finish != "length" else str(exc)
                result.ok = False
                return result
        if index > 0:
            notes.append("provider rejected response_format=json_object; retried plain and parsed the reply")
        result.notes = notes
        result.error = None if finish != "length" else "completion hit the token limit"
        result.ok = finish != "length"
        return result
    result.ok = False
    result.error = result.error or last_error or "request failed"
    result.notes = notes
    return result


async def chat_json(cfg: ChatConfig, system: str, user: str, *, max_chars: int | None = None) -> ChatResult:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if max_chars:
        messages[-1]["content"] = messages[-1]["content"][:max_chars]
    return await chat(cfg, messages)


async def test_connection(cfg: ChatConfig) -> dict[str, Any]:
    """Real reachability probe: models endpoint first, then a 1-token completion."""
    base = normalize_base_url(cfg.base_url)
    out: dict[str, Any] = {
        "baseUrl": base,
        "model": cfg.model,
        "keyConfigured": cfg.key_present,
        "keyPreview": redacted_key(cfg.api_key),
        "steps": [],
        "ok": False,
    }
    if not base:
        out["error"] = "no base URL configured"
        return out

    probes: list[tuple[str, str, dict[str, Any] | None]] = [("GET", f"{base}/models", None)]
    body = {"model": cfg.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 4}
    probes.append(("POST", _endpoint(base), {**body, "response_format": {"type": "json_object"}}))
    for method, url, payload in probes:
        step: dict[str, Any] = {"method": method, "url": url, "status": None, "body": "", "latencyMs": None,
                                "error": None}
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=min(cfg.timeout, 30.0), follow_redirects=True) as client:
                response = await (client.get(url) if payload is None else client.post(
                    url, headers=_headers(cfg.api_key), json=payload))
            step["status"] = response.status_code
            step["latencyMs"] = round((time.perf_counter() - started) * 1000)
            step["body"] = response.text[:MAX_BODY_ECHO]
            if response.status_code == 200:
                out["ok"] = True
                out["steps"].append(step)
                break
        except httpx.HTTPError as exc:
            step["error"] = f"{type(exc).__name__}: {exc}"
            step["latencyMs"] = round((time.perf_counter() - started) * 1000)
        out["steps"].append(step)
    out["error"] = None if out["ok"] else (
        out["steps"][-1].get("error") or f"HTTP {out['steps'][-1].get('status')}" if out["steps"] else "unreachable"
    )
    return out
