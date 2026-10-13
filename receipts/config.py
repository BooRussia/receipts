"""Settings: defaults, load/save to a JSON file in the user's app-data folder."""

import copy
import json
import os
import sys
from pathlib import Path

APP_NAME = "Receipts"

if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
elif sys.platform == "darwin":
    CONFIG_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
else:
    CONFIG_DIR = Path.home() / ".config" / APP_NAME

CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "receipts.log"

# Each preset is one "mode". Its hotkey grabs the last N seconds of audio and runs
# this prompt on it. Edit these in config.json to change the vibe.
DEFAULT_PRESETS = [
    {
        "id": "factcheck",
        "name": "Fact-check",
        "hotkey": "<ctrl>+<alt>+f",
        "prompt": (
            "Fact-check the claim being argued. Use this exact format:\n"
            "**Claim:** one-line paraphrase of what they said\n"
            "**Verdict:** ✅ TRUE | ❌ FALSE | ⚠️ MISLEADING | 🤷 UNPROVEN, then a short reason\n"
            "Then 2-4 short bullets with the key facts (numbers, dates, who says so).\n"
            "End with **Say this:** one or two punchy sentences the user can read out loud to settle it."
        ),
    },
    {
        "id": "argue",
        "name": "Argue back",
        "hotkey": "<ctrl>+<alt>+d",
        "prompt": (
            "Act as the user's debate coach and argue AGAINST the speaker's position. Lead with the "
            "single strongest counterpoint, then 2-3 more as short bullets. Every factual statement "
            "must be real and backed by your searches; never invent numbers. If the speaker is "
            "factually right, admit it in one line, then go after their reasoning, framing or "
            "conclusions instead.\n"
            "End with **Say this:** one or two sentences the user can say out loud."
        ),
    },
    {
        "id": "roast",
        "name": "Roast",
        "hotkey": "<ctrl>+<alt>+r",
        "prompt": (
            "Settle it with a friendly roast. First line: **Verdict:** ✅ TRUE | ❌ FALSE | "
            "⚠️ MISLEADING | 🤷 UNPROVEN. Then a short, funny roast (under 80 words) built "
            "entirely on the real facts you found. Clever, not cruel, and nothing about looks or "
            "identity. If the speaker is actually right, roast the user for doubting them instead."
        ),
    },
]

DEFAULTS = {
    # "" = auto: use the SteelSeries Sonar "Chat" output if it exists, else the default output.
    "audio_source": "",
    "listen_seconds": 45,
    "stt_provider": "local",  # local | openai | groq
    "local_model": "small.en",
    "local_device": "cpu",  # cpu | cuda (NVIDIA GPU + CUDA 12 libraries)
    "openai_api_key": "",
    "openai_stt_model": "gpt-4o-mini-transcribe",
    "groq_api_key": "",
    "groq_stt_model": "whisper-large-v3-turbo",
    "ai_provider": "claude",  # claude | grok
    "anthropic_api_key": "",
    "claude_model": "claude-opus-5-5",
    "effort": "low",
    "xai_api_key": "",
    "grok_model": "grok-4.7",
    "check_updates": True,
    "toggle_hotkey": "<ctrl>+<alt>+w",
    "presets": DEFAULT_PRESETS,
    "window": {"x": None, "y": None, "w": 440, "h": 640},
}


def load(strict: bool = False) -> dict:
    """Defaults merged with config.json. With strict=True a broken file raises instead of being ignored."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except OSError:
        saved = {}
    except ValueError:
        if strict:
            raise
        CONFIG_PATH.replace(CONFIG_PATH.with_suffix(".broken.json"))  # keep the user's edits around
        saved = {}
    for key, value in saved.items():
        if key == "window" and isinstance(value, dict):
            cfg["window"].update(value)
        elif key in cfg:
            cfg[key] = value
    presets = cfg["presets"] if isinstance(cfg["presets"], list) else []
    cfg["presets"] = [p for p in presets if isinstance(p, dict)
                      and all(isinstance(p.get(k), str) for k in ("id", "name", "prompt"))]
    if not cfg["presets"]:
        cfg["presets"] = copy.deepcopy(DEFAULT_PRESETS)
    return cfg


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def anthropic_key(cfg: dict) -> str:
    return cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
