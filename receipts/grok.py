"""Grok backend: xAI Responses API with server-side web search, streamed over SSE."""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Callable

from .llm import SYSTEM_PROMPT, Emit

log = logging.getLogger(__name__)

URL = "https://api.x.ai/v1/responses"


class GrokError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


def api_key(cfg: dict) -> str:
    return cfg.get("xai_api_key") or os.environ.get("XAI_API_KEY", "")


def stream_reply(cfg: dict, messages: list, emit: Emit, cancelled: Callable[[], bool]) -> None:
    key = api_key(cfg)
    if not key:
        raise GrokError(401, "no API key")
    body = {
        "model": (cfg.get("grok_model") or "grok-4.7").strip(),
        "input": [{"role": "system", "content": SYSTEM_PROMPT}]
        + [{"role": m["role"], "content": _plain(m["content"])} for m in messages if m["role"] in ("user", "assistant")],
        "tools": [{"type": "web_search"}],
        "include": ["no_inline_citations"],  # sources are listed under the answer instead
        "stream": True,
    }
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    })
    emit("status", "Thinking…")
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        raise GrokError(e.code, _error_text(e.read())) from e

    text: list[str] = []
    cited: dict[str, str] = {}
    final: dict = {}
    with resp:
        for kind, data in _events(resp):
            if cancelled():
                return
            if kind == "response.output_text.delta":
                if not text:
                    emit("status", "")
                text.append(data.get("delta", ""))
                emit("text", data.get("delta", ""))
            elif kind in ("response.output_item.added", "response.output_item.done"):
                item = data.get("item") or {}
                if str(item.get("type", "")).endswith("_call"):  # web_search_call and friends
                    if kind.endswith("added"):
                        emit("status", "Searching…")
                    elif query := _query(item):
                        emit("search", query)
            elif kind == "response.output_text.annotation.added":
                _add_source(cited, data.get("annotation"))
            elif kind == "response.completed":
                final = data.get("response") or {}
            elif kind in ("response.failed", "error"):
                err = (data.get("response") or {}).get("error") or data.get("error") or data
                raise GrokError(500, err.get("message", str(err)) if isinstance(err, dict) else str(err))

    for item in final.get("output") or []:
        for part in item.get("content") or [] if isinstance(item.get("content"), list) else []:
            if not text and part.get("type") == "output_text":  # no deltas arrived; use the final text
                text.append(part.get("text", ""))
                emit("text", part.get("text", ""))
            for ann in part.get("annotations") or []:
                _add_source(cited, ann)
    if not cited:  # fall back to everything the search touched
        for c in final.get("citations") or []:
            _add_source(cited, c if isinstance(c, dict) else {"url": c})

    messages.append({"role": "assistant", "content": "".join(text)})
    emit("status", "")
    if final.get("status") == "incomplete":
        emit("notice", "The answer got cut off.")
    emit("sources", list(cited.items())[:5])


def friendly_error(e: Exception, cfg: dict) -> str:
    if isinstance(e, GrokError):
        if e.status == 401:
            return "No valid xAI API key. Open ⚙ Settings and paste one in (console.x.ai), or switch to Claude."
        if e.status == 404:
            return f"Grok model not found: {cfg.get('grok_model')}. Pick another in ⚙ Settings."
        if e.status == 429:
            return "Grok is rate limiting you. Give it a few seconds and try again."
        return f"Grok API error {e.status}: {e.message}"
    if isinstance(e, urllib.error.URLError):
        return "Couldn't reach the xAI API. Check your internet connection."
    if isinstance(e, TimeoutError):
        return "Grok took too long to answer. Try again."
    return f"{type(e).__name__}: {e}"


def _events(resp):
    """Yield (event type, data dict) from a server-sent-events stream."""
    event, lines = "", []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip())
        elif not line and lines:
            payload, lines = "\n".join(lines), []
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if isinstance(data, dict):
                yield data.get("type") or event, data
            event = ""


def _plain(content) -> str:
    """History can hold Claude content blocks if the provider was switched mid-thread."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict):
            parts.append(block.get("text", "") if block.get("type") == "text" else "")
        elif getattr(block, "type", "") == "text":
            parts.append(block.text)
    return "".join(parts)


def _query(item: dict) -> str | None:
    action = item.get("action")
    if isinstance(action, dict) and action.get("query"):
        return action["query"]
    args = item.get("arguments") or item.get("input")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args[:100] or None
    if isinstance(args, dict):
        return args.get("query") or args.get("q")
    return None


def _add_source(sources: dict, ann) -> None:
    if isinstance(ann, dict) and isinstance(ann.get("url"), str) and ann["url"].startswith("http"):
        sources.setdefault(ann["url"], str(ann.get("title") or ""))


def _error_text(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.decode("utf-8", "replace")[:300]
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    return str(err)[:300]
