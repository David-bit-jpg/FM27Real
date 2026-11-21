"""Model layer: one interface, many backends.

The engine asks a model for a structured reply (a JSON object matching a
schema). Backends:

- claude-cli   Claude through the local Claude Code command line (signed-in
               account, no key). No tools, no session files.
- anthropic    Anthropic API with an API key, through the official SDK
               (pip install anthropic).
- openai-compatible presets (OpenAI, DeepSeek, Qwen, Kimi, GLM, Gemini,
               OpenRouter, Ollama, custom URL) through the Chat Completions API.
- echo         offline stand-in for tests.

Settings (settings.json in the data folder, readable by this user only):
    {"provider": "deepseek", "api_key": "...", "model": "...", "base_url": "..."}
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Protocol

CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_EFFORT = "low"  # chat replies: short and latency-sensitive


class ModelError(Exception):
    pass


class Provider(Protocol):
    name: str

    def complete(self, system: str, prompt: str, schema: dict) -> dict: ...


# --- presets -------------------------------------------------------------------
# key_prefix: used to recognise a pasted key (only prefixes unique to one provider).
PRESETS = {
    "claude-cli": {"label": "Claude 账号（本机 Claude Code）", "label_en": "Claude account (local Claude Code)",
                   "kind": "claude-cli", "needs_key": False, "model": CLAUDE_MODEL},
    "anthropic": {"label": "Anthropic API", "label_en": "Anthropic API", "kind": "anthropic",
                  "needs_key": True, "key_prefix": "sk-ant-", "model": CLAUDE_MODEL},
    "openai": {"label": "OpenAI", "label_en": "OpenAI", "kind": "openai", "needs_key": True,
               "base_url": "https://api.openai.com/v1"},
    "deepseek": {"label": "DeepSeek", "label_en": "DeepSeek", "kind": "openai", "needs_key": True,
                 "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "qwen": {"label": "通义千问（阿里云百炼）", "label_en": "Qwen (Alibaba Cloud)", "kind": "openai", "needs_key": True,
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "kimi": {"label": "Kimi（月之暗面）", "label_en": "Kimi (Moonshot)", "kind": "openai", "needs_key": True,
             "base_url": "https://api.moonshot.cn/v1"},
    "glm": {"label": "智谱 GLM", "label_en": "Zhipu GLM", "kind": "openai", "needs_key": True,
            "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "gemini": {"label": "Google Gemini", "label_en": "Google Gemini", "kind": "openai", "needs_key": True,
               "key_prefix": "AIza", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "openrouter": {"label": "OpenRouter", "label_en": "OpenRouter", "kind": "openai", "needs_key": True,
                   "key_prefix": "sk-or-", "base_url": "https://openrouter.ai/api/v1"},
    "ollama": {"label": "本地模型（Ollama）", "label_en": "Local model (Ollama)", "kind": "openai",
               "needs_key": False, "base_url": "http://127.0.0.1:11434/v1"},
    "custom": {"label": "自定义（兼容 OpenAI 的接口）", "label_en": "Custom (OpenAI-compatible)", "kind": "openai",
               "needs_key": False, "base_url": ""},
}


def detect_provider(api_key: str) -> str | None:
    """The provider a key belongs to, when its prefix is unique to one provider.
    Keys are never sent anywhere to find out."""
    key = (api_key or "").strip()
    for pid, p in PRESETS.items():
        prefix = p.get("key_prefix")
        if prefix and key.startswith(prefix):
            return pid
    return None


# --- Claude Code command line ---------------------------------------------------
def find_claude_cli() -> str | None:
    """FM27REAL_CLAUDE, then `claude` on PATH, then the copy bundled with the Claude app."""
    explicit = os.environ.get("FM27REAL_CLAUDE")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    pattern = os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude")

    def version_key(path: str) -> tuple:
        version = path.split("/claude-code/")[1].split("/")[0]
        return tuple(int(p) if p.isdigit() else 0 for p in version.split("."))

    bundled = sorted(glob.glob(pattern), key=version_key)
    return bundled[-1] if bundled else None


def _clean_env() -> dict:
    """Environment without variables inherited from a Claude host process (they
    make the CLI wait for host-provided credentials) or the game's injection."""
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("CLAUDE") or k in ("ANTHROPIC_BASE_URL", "DYLD_INSERT_LIBRARIES"))}


class ClaudeCLI:
    name = "claude-cli"

    def __init__(self, model: str = CLAUDE_MODEL, effort: str = CLAUDE_EFFORT, timeout: float = 120):
        self.path = find_claude_cli()
        self.model = model or CLAUDE_MODEL
        self.effort = effort
        self.timeout = timeout

    def complete(self, system: str, prompt: str, schema: dict) -> dict:
        if not self.path:
            raise ModelError("Claude command line not found (install Claude, or set FM27REAL_CLAUDE)")
        args = [self.path, "-p", prompt, "--system-prompt", system, "--json-schema", json.dumps(schema),
                "--output-format", "json", "--tools", "", "--no-session-persistence",
                "--model", self.model, "--effort", self.effort]
        with tempfile.TemporaryDirectory(prefix="fm27real-") as cwd:  # no project files picked up
            try:
                proc = subprocess.run(args, cwd=cwd, env=_clean_env(), capture_output=True,
                                      text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired as e:
                raise ModelError(f"model timed out after {self.timeout:.0f}s") from e
        try:
            out = json.loads(proc.stdout)
        except ValueError as e:
            raise ModelError(f"unexpected CLI output: {(proc.stdout or proc.stderr)[:300]}") from e
        if out.get("is_error"):
            raise ModelError(str(out.get("result") or out.get("subtype") or "model error"))
        for key in ("structured_output", "structuredOutput"):
            if isinstance(out.get(key), dict):
                return out[key]
        return parse_json_object(out.get("result") or "")


# --- Anthropic API (official SDK) -------------------------------------------------
class AnthropicAPI:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = CLAUDE_MODEL, effort: str = CLAUDE_EFFORT, timeout: float = 120):
        self.api_key = api_key
        self.model = model or CLAUDE_MODEL
        self.effort = effort
        self.timeout = timeout

    def _client(self):
        try:
            import anthropic
        except ImportError as e:
            raise ModelError("the Anthropic SDK is not installed: pip install anthropic") from e
        return anthropic, anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout)

    def complete(self, system: str, prompt: str, schema: dict) -> dict:
        anthropic, client = self._client()
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                # On a safety decline the API retries on a fallback model in the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise ModelError("invalid API key") from e
        except anthropic.PermissionDeniedError as e:
            raise ModelError("the API key lacks permission for this model") from e
        except anthropic.NotFoundError as e:
            raise ModelError(f"unknown model: {self.model}") from e
        except anthropic.RateLimitError as e:
            raise ModelError("rate limited; try again shortly") from e
        except anthropic.APIStatusError as e:
            raise ModelError(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ModelError("network error reaching the Anthropic API") from e
        if response.stop_reason == "refusal":
            raise ModelError("the model declined to answer")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return parse_json_object(text)

    def list_models(self) -> list[str]:
        anthropic, client = self._client()
        try:
            return [m.id for m in client.models.list()]
        except anthropic.APIError as e:
            raise ModelError(f"could not list models: {e}") from e


# --- OpenAI-compatible Chat Completions -----------------------------------------
class OpenAICompatible:
    name = "openai-compatible"

    def __init__(self, base_url: str, api_key: str = "", model: str = "", timeout: float = 120):
        if not base_url:
            raise ModelError("no API address set")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403):
                raise ModelError("invalid API key or no access") from e
            raise ModelError(f"API error {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ModelError(f"cannot reach {self.base_url}: {getattr(e, 'reason', e)}") from e

    def complete(self, system: str, prompt: str, schema: dict) -> dict:
        if not self.model:
            raise ModelError("no model chosen")
        # Not every compatible service enforces schemas; describe it and ask for a JSON object.
        system_json = (f"{system}\n\nRespond with a single JSON object (no other text) matching this JSON "
                       f"Schema:\n{json.dumps(schema, ensure_ascii=False)}")
        body = {"model": self.model,
                "messages": [{"role": "system", "content": system_json}, {"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}}
        try:
            out = self._request("POST", "/chat/completions", body)
        except ModelError as e:
            if "response_format" not in str(e) and "json" not in str(e).lower():
                raise
            body.pop("response_format")  # services without JSON mode
            out = self._request("POST", "/chat/completions", body)
        try:
            text = out["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ModelError(f"unexpected response: {str(out)[:200]}") from e
        return parse_json_object(text)

    def list_models(self) -> list[str]:
        out = self._request("GET", "/models")
        items = out.get("data") or out.get("models") or []
        return sorted({m.get("id") or m.get("name") for m in items if isinstance(m, dict)} - {None})


# --- helpers ---------------------------------------------------------------------
def parse_json_object(text: str) -> dict:
    """A JSON object from model text, tolerating code fences and stray prose."""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ModelError(f"model did not return JSON: {text[:200]}")
    try:
        value = json.loads(text[start:end + 1])
    except ValueError as e:
        raise ModelError(f"model returned invalid JSON: {text[:200]}") from e
    if not isinstance(value, dict):
        raise ModelError("model returned JSON that is not an object")
    return value


class Echo:
    """Offline stand-in: proves the pipeline without calling a model."""
    name = "echo"

    def __init__(self, reply: dict | None = None):
        self.reply = reply
        self.last_system = self.last_prompt = None

    def complete(self, system: str, prompt: str, schema: dict) -> dict:
        self.last_system, self.last_prompt = system, prompt
        return self.reply or {"reply": "（测试回复）收到，教练。", "mood": "neutral",
                              "relation_delta": {"trust": 0, "liking": 0, "respect": 0},
                              "memory": "", "new_canon": [], "end": False}


def provider_from_settings(settings: dict) -> Provider:
    pid = settings.get("provider") or "claude-cli"
    if pid == "echo":
        return Echo()
    preset = PRESETS.get(pid)
    if not preset:
        raise ModelError(f"unknown provider: {pid}")
    key = settings.get("api_key") or ""
    model = settings.get("model") or preset.get("model") or ""
    if preset["needs_key"] and not key:
        raise ModelError("no API key set (open Settings)")
    if preset["kind"] == "claude-cli":
        return ClaudeCLI(model=model)
    if preset["kind"] == "anthropic":
        return AnthropicAPI(api_key=key, model=model)
    return OpenAICompatible(base_url=settings.get("base_url") or preset.get("base_url", ""), api_key=key, model=model)
