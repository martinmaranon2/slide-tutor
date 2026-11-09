"""Claude calls through the Claude Agent SDK, i.e. the local Claude Code CLI logged in with your Claude subscription.

Every call is an isolated, single-turn, tool-less request:
  * custom system prompt (Claude Code's default prompt/tools are not used)
  * tools=[], no MCP servers (strict), no skills, no slash commands, no filesystem settings / CLAUDE.md
  * no session persistence (nothing is written to ~/.claude/projects)
Content goes in as Anthropic content blocks (base64 PNG images + text) via the SDK's streaming-input mode.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    query,
)

import storage

log = logging.getLogger("slide_tutor.llm")

NOTES_MODEL = os.environ.get("NOTES_MODEL", "claude-sonnet-5-5")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-sonnet-5-5")
NOTES_EFFORT = os.environ.get("NOTES_EFFORT", "medium")
CHAT_EFFORT = os.environ.get("CHAT_EFFORT", "medium")
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))

# Empty working directory for the CLI so no project files/CLAUDE.md are ever picked up.
AGENT_CWD = storage.STATE / "agent-cwd"
AGENT_CWD.mkdir(parents=True, exist_ok=True)

# Latest plan rate-limit info reported by the CLI: {"five_hour": {...}, "seven_day": {...}} (persisted across restarts)
RATE_FILE = storage.STATE / "rate_limits.json"
rate_limits: dict[str, dict] = storage.read_json(RATE_FILE, default={}) or {}
# Summary of the most recent CLI init message (tools / MCP servers actually loaded), for verification.
last_init: dict = {}


class LLMError(Exception):
    pass


class TransientError(LLMError):
    """Overloaded / 429 / 5xx / CLI hiccup: worth retrying with backoff."""

    def __init__(self, msg: str, partial: bool = False):
        super().__init__(msg)
        self.partial = partial  # text was already streamed to the user


class UsageLimitError(LLMError):
    """Your plan's usage limit was reached; retrying before resets_at is pointless."""

    def __init__(self, msg: str, resets_at: int | None = None):
        super().__init__(msg)
        self.resets_at = resets_at


class FatalError(LLMError):
    """Auth failure, invalid request, refusal: don't retry."""


@dataclass
class LLMResult:
    text: str
    model: str
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    duration_ms: int = 0


# ---------------------------------------------------------------- content helpers

def image_block(png: bytes) -> dict:
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": base64.standard_b64encode(png).decode("ascii")}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def describe_content(system: str, content: list[dict]) -> dict:
    """Hash-level description of a request for the verification log (no base64 dumped)."""
    blocks = []
    for b in content:
        if b["type"] == "image":
            raw = base64.standard_b64decode(b["source"]["data"])
            blocks.append({"type": "image", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        else:
            t = b["text"]
            blocks.append({"type": "text", "chars": len(t),
                           "sha256": hashlib.sha256(t.encode()).hexdigest()[:16],
                           "head": t[:160]})
    return {"system_chars": len(system),
            "system_sha256": hashlib.sha256(system.encode()).hexdigest()[:16],
            "blocks": blocks}


def sha256_file(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# ---------------------------------------------------------------- rate limits

def _update_rate_limits(info) -> None:
    raw = getattr(info, "raw", None) or {}
    windows = raw.get("unifiedWindows") or {}
    for name, w in windows.items():
        rate_limits[name] = {"utilization": w.get("utilization"), "resets_at": w.get("resetsAt"),
                             "status": w.get("status")}
    if info.rate_limit_type:
        cur = rate_limits.setdefault(info.rate_limit_type, {})
        cur["status"] = info.status
        if info.resets_at:
            cur["resets_at"] = info.resets_at
        if info.utilization is not None:
            cur["utilization"] = info.utilization
    try:
        storage.write_json(RATE_FILE, rate_limits)
    except OSError:
        pass


def _options(system: str, model: str, effort: str | None, stream: bool) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=system,
        model=model,
        effort=effort or None,
        tools=[],
        allowed_tools=[],
        mcp_servers={},
        strict_mcp_config=True,
        setting_sources=[],
        skills=[],
        max_turns=1,
        verbatim_prompts=True,
        include_partial_messages=stream,
        cwd=str(AGENT_CWD),
        extra_args={"no-session-persistence": None, "disable-slash-commands": None},
        stderr=lambda line: log.debug("cli: %s", line),
    )


_LIMIT_RE = re.compile(r"usage limit|limit reached|limit will reset|out of (extra )?usage|hit your limit", re.I)


async def run(system: str, content: list[dict], *, model: str, effort: str | None = None,
              on_delta: Callable[[str], None] | None = None) -> LLMResult:
    """One isolated request. Streams text deltas to on_delta (if given). Raises an LLMError subclass on failure."""
    t0 = time.monotonic()

    async def prompt():
        yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

    streamed: list[str] = []
    assistant_text: list[str] = []
    assistant_error: str | None = None
    limit_hit = None
    result: ResultMessage | None = None
    try:
        async for m in query(prompt=prompt(), options=_options(system, model, effort, on_delta is not None)):
            if isinstance(m, StreamEvent):
                ev = m.event
                if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    t = ev["delta"].get("text", "")
                    streamed.append(t)
                    if on_delta and t:
                        on_delta(t)
            elif isinstance(m, AssistantMessage):
                if m.error:
                    assistant_error = m.error
                assistant_text.extend(b.text for b in m.content if isinstance(b, TextBlock))
            elif isinstance(m, RateLimitEvent):
                _update_rate_limits(m.rate_limit_info)
                if m.rate_limit_info.status == "rejected":
                    limit_hit = m.rate_limit_info
            elif isinstance(m, SystemMessage) and m.subtype == "init":
                d = m.data
                last_init.update({"model": d.get("model"), "tools": d.get("tools"),
                                  "mcp_servers": d.get("mcp_servers"), "apiKeySource": d.get("apiKeySource"),
                                  "skills": len(d.get("skills") or [])})
            elif isinstance(m, ResultMessage):
                result = m
    except CLINotFoundError as e:
        raise FatalError("Claude Code CLI not found. Install Claude Code and run `claude` once to log in.") from e
    except Exception as e:
        # On an API error the CLI first emits an error result and then exits non-zero, so the SDK raises *after*
        # yielding that result: classify from the result below. Without one it was a process hiccup (crash,
        # init timeout) and worth a retry.
        if result is None and not assistant_error:
            if limit_hit:
                raise UsageLimitError("Plan usage limit reached", limit_hit.resets_at) from e
            raise TransientError(f"Claude Code process error: {e}", partial=bool(streamed)) from e
        log.debug("Claude Code exited with an error after its result: %s", e)

    if result is None and not assistant_error:
        raise TransientError("no result from Claude Code", partial=bool(streamed))

    text = "".join(assistant_text).strip() or "".join(streamed).strip() or ((result and result.result) or "").strip()
    if result is None or result.is_error or assistant_error:
        detail = ((result.result or "") + " " + " ".join(result.errors or [])) if result else ""
        detail = detail.strip() or assistant_error or (result.subtype if result else "error")
        status = result.api_error_status if result else None
        if limit_hit or (assistant_error == "rate_limit" and _LIMIT_RE.search(detail)) or _LIMIT_RE.search(detail):
            resets = limit_hit.resets_at if limit_hit else (rate_limits.get("five_hour") or {}).get("resets_at")
            raise UsageLimitError(f"Plan usage limit reached: {detail[:200]}", resets)
        if assistant_error in ("authentication_failed", "billing_error"):
            raise FatalError(f"{assistant_error}: {detail[:300]} (run `claude` and log in again)")
        if assistant_error == "invalid_request" or status == 400:
            raise FatalError(f"invalid request: {detail[:300]}")
        raise TransientError(f"API error {status or assistant_error or ''}: {detail[:300]}", partial=bool(streamed))
    if result.stop_reason == "refusal":
        raise FatalError("The model declined this request.")
    if not text:
        raise TransientError("empty response", partial=bool(streamed))

    return LLMResult(text=text, model=model, usage=result.usage or {},
                     cost_usd=float(result.total_cost_usd or 0.0),
                     duration_ms=result.duration_ms or int((time.monotonic() - t0) * 1000))


async def run_with_retries(system: str, content: list[dict], *, model: str, effort: str | None = None,
                           on_delta: Callable[[str], None] | None = None,
                           on_retry: Callable[[int, float, Exception], None] | None = None,
                           attempts: int = MAX_ATTEMPTS) -> LLMResult:
    """Exponential backoff with jitter on transient errors (2s, 4s, 8s, 16s, ... capped at 60s).
    Never retries once text has been streamed to the user (would duplicate output)."""
    for attempt in range(attempts):
        try:
            return await run(system, content, model=model, effort=effort, on_delta=on_delta)
        except TransientError as e:
            if e.partial or attempt == attempts - 1:
                raise
            delay = min(60.0, 2.0 * 2 ** attempt) + random.uniform(0, 1.0)
            log.warning("transient error (attempt %d/%d), retrying in %.1fs: %s", attempt + 1, attempts, delay, e)
            if on_retry:
                on_retry(attempt + 1, delay, e)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
