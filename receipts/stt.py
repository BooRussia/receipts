"""Speech-to-text: local Whisper (free, private) or a cloud API.

Every provider returns a Transcript made of short lines, so any line can be tapped and checked on
its own. xAI also labels who said each line (diarization), so one voice can be picked out of a
busy channel.
"""

import io
import json
import logging
import re
import threading
from dataclasses import dataclass
import urllib.error
import urllib.request
import uuid
import wave

import numpy as np

from .audio import SAMPLE_RATE

log = logging.getLogger(__name__)

CLOUD_URLS = {
    "openai": "https://api.openai.com/v1/audio/transcriptions",
    "groq": "https://api.groq.com/openai/v1/audio/transcriptions",
    "xai": "https://api.x.ai/v1/stt",
}
PROVIDER_NAMES = {"openai": "OpenAI", "groq": "Groq", "xai": "xAI"}

SILENCE_RMS = 0.002  # below this the clip is effectively silent
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Line:
    text: str
    speaker: int | None = None  # 1, 2, 3… within one clip; None when the provider can't tell


@dataclass
class Transcript:
    lines: list[Line]

    @property
    def speakers(self) -> list[int]:
        return sorted({ln.speaker for ln in self.lines if ln.speaker is not None})

    @property
    def text(self) -> str:
        return " ".join(ln.text for ln in self.lines).strip()

    def for_ai(self) -> str:
        """Plain text, or one "S1: …" row per speaker turn when speakers are known."""
        if not self.speakers:
            return self.text
        rows: list[list] = []
        for ln in self.lines:
            if rows and rows[-1][0] == ln.speaker:
                rows[-1][1].append(ln.text)
            else:
                rows.append([ln.speaker, [ln.text]])
        return "\n".join(f"S{spk or '?'}: {' '.join(parts)}" for spk, parts in rows)


def _sentences(text: str, speaker: int | None = None) -> list[Line]:
    return [Line(t.strip(), speaker) for t in _SENTENCE_END.split(text or "") if t.strip()]


def _from_words(words: list[dict]) -> list[Line]:
    """Group diarized words into lines: a new line at each speaker change, sentence end or long pause."""
    order: dict = {}  # raw speaker ids → 1, 2, 3 in order of first appearance
    lines: list[Line] = []
    current: list[str] = []
    speaker, last_end = None, 0.0
    for w in words:
        text = str(w.get("text", "")).strip()
        if not text:
            continue
        raw = w.get("speaker")
        spk = order.setdefault(raw, len(order) + 1) if raw is not None else None
        gap = float(w.get("start") or 0) - last_end
        if current and (spk != speaker or gap > 1.5 or (current[-1][-1:] in ".!?" and len(current) >= 4)):
            lines.append(Line(" ".join(current), speaker))
            current = []
        current.append(text)
        speaker, last_end = spk, float(w.get("end") or last_end)
    if current:
        lines.append(Line(" ".join(current), speaker))
    return lines


class Transcriber:
    def __init__(self):
        self._model = None
        self._model_key = None
        self._lock = threading.Lock()

    def has_speech(self, audio: np.ndarray) -> bool:
        # Look at the loudest half second so a short sentence inside a long silent clip still counts.
        win = SAMPLE_RATE // 2
        if len(audio) < win:
            return False
        frames = audio[: len(audio) // win * win].reshape(-1, win)
        return float(np.sqrt(np.mean(np.square(frames), axis=1)).max()) > SILENCE_RMS

    def transcribe(self, audio: np.ndarray, cfg: dict) -> Transcript:
        provider = cfg.get("stt_provider", "local")
        if provider == "local":
            return self._local(audio, cfg)
        return self._cloud(audio, cfg, provider)

    # --- local ---------------------------------------------------------------

    def preload(self, cfg: dict) -> None:
        if cfg.get("stt_provider", "local") == "local":
            self._load_local(cfg)

    def is_loaded(self, cfg: dict) -> bool:
        return self._model_key == (cfg["local_model"], cfg["local_device"])

    def _load_local(self, cfg: dict):
        key = (cfg["local_model"], cfg["local_device"])
        with self._lock:
            if self._model_key != key:
                from faster_whisper import WhisperModel

                name, device = key
                log.info("Loading Whisper model %s on %s (first time downloads it)", name, device)
                compute = "float16" if device == "cuda" else "int8"
                self._model = WhisperModel(name, device=device, compute_type=compute)
                self._model_key = key
            return self._model

    def _local(self, audio: np.ndarray, cfg: dict) -> Transcript:
        model = self._load_local(cfg)
        english_only = cfg["local_model"].endswith(".en")
        segments, _ = model.transcribe(
            audio,
            language="en" if english_only else None,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        # Whisper's segments can break mid-sentence; re-split on real sentence endings instead.
        return Transcript(_sentences(" ".join(s.text.strip() for s in segments)))

    # --- cloud ---------------------------------------------------------------

    def _cloud(self, audio: np.ndarray, cfg: dict, provider: str) -> Transcript:
        name = PROVIDER_NAMES[provider]
        key = cfg.get(f"{provider}_api_key", "")
        if not key:
            raise RuntimeError(f"Add your {name} API key in ⚙ Settings (or switch transcription to Local).")
        fields = {"model": cfg[f"{provider}_stt_model"]}
        if provider == "xai":
            fields.update(language="en", format="true", diarize="true")  # diarize: label each speaker
        elif provider == "groq":
            fields["response_format"] = "verbose_json"  # adds per-sentence segments
        else:
            fields["response_format"] = "json"
        body, content_type = _multipart(fields, ("file", "clip.wav", _wav_bytes(audio), "audio/wav"))
        req = urllib.request.Request(
            CLOUD_URLS[provider],
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{name} transcription failed ({e.code}): {detail}") from e
        return parse_response(data)


def parse_response(data: dict) -> Transcript:
    if isinstance(data.get("words"), list) and data["words"]:
        return Transcript(_from_words(data["words"]))
    if isinstance(data.get("segments"), list) and data["segments"]:
        return Transcript(_sentences(" ".join(str(s.get("text", "")).strip() for s in data["segments"])))
    return Transcript(_sentences(data.get("text", "")))


def _wav_bytes(audio: np.ndarray) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return out.getvalue()


def _multipart(fields: dict, file: tuple) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    field, filename, data, mime = file
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode() + data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
