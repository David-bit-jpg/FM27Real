"""Model layer: one interface, swappable backends.

The engine asks a model for a structured reply (JSON matching a schema). The
test backend is Claude through the locally installed Claude Code command line,
using the signed-in account; no tools, no session files, nothing but text.

Backends:
- ClaudeCLI: `claude -p` with --json-schema (default)
- Echo: offline stand-in for tests (returns a canned reply)
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
from typing import Protocol

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "low"  # chat replies: short, latency-sensitive


class ModelError(Exception):
    pass


class Provider(Protocol):
    name: str

    def complete(self, system: str, prompt: str, schema: dict) -> dict: ...


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
    """Environment for the CLI without variables inherited from a Claude host
    process (they make it wait for host-provided credentials)."""
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("CLAUDE") or k in ("ANTHROPIC_BASE_URL",))}


class ClaudeCLI:
    name = "claude-cli"

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT, timeout: float = 120):
        self.path = find_claude_cli()
        self.model = model
        self.effort = effort
        self.timeout = timeout

    def complete(self, system: str, prompt: str, schema: dict) -> dict:
        if not self.path:
            raise ModelError("Claude command line not found (install Claude, or set FM27REAL_CLAUDE)")
        args = [
            self.path, "-p", prompt,
            "--system-prompt", system,
            "--json-schema", json.dumps(schema),
            "--output-format", "json",
            "--tools", "",
            "--no-session-persistence",
            "--model", self.model,
            "--effort", self.effort,
        ]
        # An empty working directory, so no project files are picked up.
        with tempfile.TemporaryDirectory(prefix="fm27real-") as cwd:
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
        return parse_structured(out)


def parse_structured(out: dict) -> dict:
    """The structured result, wherever this CLI version puts it."""
    for key in ("structured_output", "structuredOutput"):
        if isinstance(out.get(key), dict):
            return out[key]
    text = (out.get("result") or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        value = json.loads(text)
    except ValueError as e:
        raise ModelError(f"model did not return JSON: {text[:200]}") from e
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
    backend = settings.get("model_backend", "claude-cli")
    if backend == "echo":
        return Echo()
    return ClaudeCLI(model=settings.get("model", DEFAULT_MODEL), effort=settings.get("effort", DEFAULT_EFFORT))
