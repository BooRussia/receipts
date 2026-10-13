"""The fact-checking brain: shared prompt, plus a Claude or Grok backend."""

from typing import Callable

SYSTEM_PROMPT = """\
You are Receipts, a fact-checker that pops up on the user's screen during live voice-chat \
arguments (usually Discord). The user has a friend who loves to argue, and the user wants to \
win with facts, fast.

What you get: a rough speech-to-text transcript of the last stretch of the voice channel. It \
contains only the OTHER people in the call (the user's own mic is not recorded), has no speaker \
labels, may include several people, and will have transcription mistakes. Read it charitably: \
fix obvious mis-hearings, ignore filler and game chatter, and find the claim actually being \
argued, usually the most recent confident factual assertion. If there are two separate claims, \
handle the main one and mention the other in one line.

How to work:
- Search the web before giving a verdict on anything checkable. Prefer authoritative sources \
(official statistics, encyclopedias, reputable news outlets, expert bodies).
- Be honest. If the speaker is actually right, say so plainly; a wrong "gotcha" is worse than none.
- Pure opinion or taste can't be fact-checked: say that in one line, then give the strongest \
factual points either way.
- If the transcript holds no real claim (silence, game callouts, music), say so in one short \
line and suggest the user type the claim instead.

Output: the user reads this mid-conversation, so make it glanceable. Short lines, bold labels, \
no preamble, no headings, no links in the text (sources are listed separately). Stay under \
about 150 words unless asked for more. Later messages come from the user; answer them just as \
briefly."""

PROVIDERS = {"claude": "Claude (Anthropic)", "grok": "Grok (xAI)"}

# Events a backend emits while it works:
#   ("status", str)  ("search", query)  ("text", delta)  ("reset", None) to discard partial text
#   ("sources", [(url, title)])  ("notice", str)
Emit = Callable[[str, object], None]


def first_message(preset: dict, *, transcript: str | None = None, seconds: int = 0, typed: str = "") -> str:
    if transcript is not None:
        body = f'<transcript seconds="{seconds}">\n{transcript}\n</transcript>'
    else:
        body = f"The user typed the claim themselves:\n<claim>\n{typed}\n</claim>"
    return f"Mode: {preset['name']}\n{preset['prompt']}\n\n{body}"


def provider_key(cfg: dict) -> str:
    if cfg.get("ai_provider") == "grok":
        from .grok import api_key
    else:
        from .claude import api_key
    return api_key(cfg)


def stream_reply(cfg: dict, messages: list, emit: Emit, cancelled: Callable[[], bool]) -> None:
    """Run one assistant turn on `messages`, appending the reply to it and streaming to `emit`."""
    if cfg.get("ai_provider") == "grok":
        from .grok import stream_reply as run
    else:
        from .claude import stream_reply as run
    run(cfg, messages, emit, cancelled)


def friendly_error(e: Exception, cfg: dict) -> str:
    if cfg.get("ai_provider") == "grok":
        from .grok import friendly_error as explain
    else:
        from .claude import friendly_error as explain
    return explain(e, cfg)
