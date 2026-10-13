"""Speech-to-text: local Whisper (free, private) or a cloud Whisper API (faster on weak PCs)."""

import io
import json
import logging
import threading
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
}

SILENCE_RMS = 0.002  # below this the clip is effectively silent


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

    def transcribe(self, audio: np.ndarray, cfg: dict) -> str:
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

    def _local(self, audio: np.ndarray, cfg: dict) -> str:
        model = self._load_local(cfg)
        english_only = cfg["local_model"].endswith(".en")
        segments, _ = model.transcribe(
            audio,
            language="en" if english_only else None,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    # --- cloud ---------------------------------------------------------------

    def _cloud(self, audio: np.ndarray, cfg: dict, provider: str) -> str:
        key = cfg.get(f"{provider}_api_key", "")
        if not key:
            raise RuntimeError(f"Add your {provider.title()} API key in Settings (or switch transcription to Local).")
        body, content_type = _multipart(
            {"model": cfg[f"{provider}_stt_model"], "response_format": "json"},
            ("file", "clip.wav", _wav_bytes(audio), "audio/wav"),
        )
        req = urllib.request.Request(
            CLOUD_URLS[provider],
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read()).get("text", "").strip()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{provider.title()} transcription failed ({e.code}): {detail}") from e


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
