"""Always-on capture of one audio source into a rolling in-memory buffer.

On Windows, every output device can be recorded with WASAPI loopback, so picking the
"SteelSeries Sonar - Chat" output records exactly what Discord plays and nothing else.
Nothing is written to disk; the buffer only ever holds the last couple of minutes.
"""

import logging
import sys
import threading
import warnings

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # what Whisper wants; WASAPI resamples for us
BUFFER_SECONDS = 180
IS_WINDOWS = sys.platform == "win32"


def _soundcard():
    import soundcard

    warnings.filterwarnings("ignore", message="data discontinuity")
    return soundcard


def _all_devices():
    sc = _soundcard()
    return sc.all_microphones(include_loopback=True) if IS_WINDOWS else sc.all_microphones()


def _key(dev) -> str:
    return ("out:" if getattr(dev, "isloopback", False) else "in:") + dev.name


def list_sources() -> list[tuple[str, str]]:
    """(key, label) for every source we can record."""
    out = []
    for dev in _all_devices():
        if getattr(dev, "isloopback", False):
            out.append((_key(dev), f"🔊 {dev.name}  (what it plays)"))
        else:
            out.append((_key(dev), f"🎤 {dev.name}"))
    return out


def _resolve(key: str):
    devices = _all_devices()
    if key:
        for dev in devices:
            if _key(dev) == key:
                return dev
        raise RuntimeError(f"Audio source not found: {key.split(':', 1)[-1]}")
    # Auto: Sonar's Chat channel if present, otherwise whatever is playing on the default output.
    for dev in devices:
        name = dev.name.lower()
        if getattr(dev, "isloopback", False) and "sonar" in name and "chat" in name:
            return dev
    sc = _soundcard()
    if IS_WINDOWS:
        default_out = sc.default_speaker().name
        for dev in devices:
            if getattr(dev, "isloopback", False) and dev.name == default_out:
                return dev
    return sc.default_microphone()


class _Ring:
    def __init__(self, size: int):
        self.buf = np.zeros(size, dtype=np.float32)
        self.pos = 0
        self.filled = 0
        self.lock = threading.Lock()

    def write(self, x: np.ndarray) -> None:
        n, size = len(x), len(self.buf)
        with self.lock:
            if n >= size:
                self.buf[:] = x[-size:]
                self.pos, self.filled = 0, size
                return
            end = self.pos + n
            if end <= size:
                self.buf[self.pos:end] = x
            else:
                k = size - self.pos
                self.buf[self.pos:] = x[:k]
                self.buf[: n - k] = x[k:]
            self.pos = end % size
            self.filled = min(size, self.filled + n)

    def last(self, n: int) -> np.ndarray:
        size = len(self.buf)
        with self.lock:
            n = min(n, self.filled)
            start = (self.pos - n) % size
            if start + n <= size:
                return self.buf[start:start + n].copy()
            return np.concatenate([self.buf[start:], self.buf[: start + n - size]])

    def clear(self) -> None:
        with self.lock:
            self.pos = self.filled = 0


class AudioListener:
    """Records the chosen source on a background thread, forever."""

    def __init__(self):
        self._ring = _Ring(BUFFER_SECONDS * SAMPLE_RATE)
        self._key = ""
        self._switch = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audio", daemon=True)
        self.level = 0.0  # recent peak RMS, for the UI meter
        self.source_name = ""
        self.error = ""

    def start(self, key: str) -> None:
        self._key = key
        self._thread.start()

    def set_source(self, key: str) -> None:
        if key != self._key:
            self._key = key
            self._switch.set()

    def stop(self) -> None:
        self._stop.set()
        self._switch.set()

    def last(self, seconds: float) -> np.ndarray:
        return self._ring.last(int(seconds * SAMPLE_RATE))

    def _run(self) -> None:
        if IS_WINDOWS:
            # WASAPI is COM; this thread needs its own COM apartment.
            import ctypes

            ctypes.windll.ole32.CoInitializeEx(None, 0)
        block = SAMPLE_RATE // 10
        while not self._stop.is_set():
            self._switch.clear()
            try:
                dev = _resolve(self._key)
                self.source_name, self.error = dev.name, ""
                self._ring.clear()
                log.info("Listening to %r", dev)
                # WASAPI takes the block size as its buffer length; CoreAudio caps it at 512 frames.
                with dev.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=block if IS_WINDOWS else None) as rec:
                    while not self._switch.is_set():
                        data = rec.record(numframes=block)
                        mono = data[:, 0] if data.ndim == 2 else data
                        self._ring.write(mono.astype(np.float32, copy=False))
                        rms = float(np.sqrt(np.mean(np.square(mono)))) if len(mono) else 0.0
                        self.level = max(rms, self.level * 0.8)
            except Exception as e:
                log.exception("Audio capture failed")
                self.source_name, self.error, self.level = "", str(e) or type(e).__name__, 0.0
                self._switch.wait(3)
