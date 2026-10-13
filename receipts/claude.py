"""Claude backend: Anthropic API with server-side web search, streamed."""

import logging
from typing import Callable

import anthropic

from .config import anthropic_key
from .llm import SYSTEM_PROMPT, Emit

log = logging.getLogger(__name__)

# Models that take the newer web search tool (with dynamic result filtering).
_DYNAMIC_SEARCH_MODELS = (
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
)
# Models where a safety-classifier decline can be retried server-side on another model.
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")
_NO_EFFORT_MODELS = ("claude-haiku-4", "claude-sonnet-4-5", "claude-3")


def _request(cfg: dict, messages: list) -> dict:
    model = cfg["claude_model"].strip()
    search = "web_search_20260209" if model in _DYNAMIC_SEARCH_MODELS else "web_search_20250305"
    params = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=[{"type": search, "name": "web_search", "max_uses": 5}],
        messages=messages,
        cache_control={"type": "ephemeral"},
    )
    if not model.startswith(_NO_EFFORT_MODELS):
        params["output_config"] = {"effort": cfg.get("effort", "low")}
    if model in _FALLBACK_MODELS:
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"
    return params


def stream_reply(cfg: dict, messages: list, emit: Emit, cancelled: Callable[[], bool]) -> None:
    client = anthropic.Anthropic(api_key=api_key(cfg) or None, timeout=120)
    params = _request(cfg, messages)
    sources: dict[str, str] = {}
    final = None
    emit("status", "Thinking…")
    for _ in range(5):  # extra rounds only when the server pauses a long search turn
        with client.beta.messages.stream(**params) as stream:
            for event in stream:
                if cancelled():
                    return  # leaving the `with` closes the connection
                if event.type == "content_block_start":
                    kind = event.content_block.type
                    if kind == "server_tool_use":
                        emit("status", "Searching…")
                    elif kind == "web_search_tool_result":
                        emit("status", "Reading results…")
                    elif kind == "fallback":
                        emit("reset", None)  # the first model declined mid-answer; start over
                    elif kind == "text":
                        emit("status", "")
                elif event.type == "content_block_stop":
                    block = event.content_block
                    if block.type == "server_tool_use" and block.name == "web_search":
                        query = block.input.get("query") if isinstance(block.input, dict) else None
                        if query:
                            emit("search", query)
                elif event.type == "text":
                    emit("text", event.text)
            final = stream.get_final_message()
        messages.append({"role": "assistant", "content": final.content})
        for block in final.content:
            if block.type == "text":
                for cite in block.citations or []:
                    if cite.type == "web_search_result_location":
                        sources.setdefault(cite.url, cite.title or cite.url)
        if final.stop_reason != "pause_turn":
            break

    emit("status", "")
    if final.stop_reason == "refusal":
        emit("notice", "Claude declined to answer this one.")
    elif final.stop_reason == "max_tokens":
        emit("notice", "The answer got cut off.")
    emit("sources", list(sources.items())[:5])


def api_key(cfg: dict) -> str:
    return anthropic_key(cfg)


def friendly_error(e: Exception, cfg: dict) -> str:
    if isinstance(e, anthropic.AuthenticationError) or (not api_key(cfg) and isinstance(e, TypeError)):
        return "No valid Anthropic API key. Open ⚙ Settings and paste one in (console.anthropic.com), or switch to Grok."
    if isinstance(e, anthropic.NotFoundError):
        return f"Model not found: {cfg['claude_model']}. Pick another in ⚙ Settings."
    if isinstance(e, anthropic.RateLimitError):
        return "Rate limited by the API. Give it a few seconds and try again."
    if isinstance(e, anthropic.BadRequestError):
        return f"The API rejected the request: {e.message}"
    if isinstance(e, anthropic.APIStatusError):
        return f"API error {e.status_code}: {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach the Anthropic API. Check your internet connection."
    return f"{type(e).__name__}: {e}"
