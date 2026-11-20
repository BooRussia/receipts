"""Live recording: keep transcribing both sides of the call until the user stops it.

Your mic is "You" and the voice-chat output is "Them". Each side is cut into statements at natural
pauses and transcribed in the background, so any recent statement can be picked out and checked
while the recording keeps going. Everything stays in memory unless the user saves it.
"""

import bisect
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .audio import SAMPLE_RATE

log = logging.getLogger(__name__)

BLOCK = SAMPLE_RATE // 10  # look at the audio 100 ms at a time
PAUSE_BLOCKS = 7  # 0.7 s of quiet ends a statement
PREROLL_BLOCKS = 3  # keep 0.3 s from just before speech starts
MAX_BLOCKS = 200  # cut anyway after 20 s of non-stop talking
MIN_SPEECH_BLOCKS = 4  # ignore blips under 0.4 s
# Whisper's classic inventions for near-silent audio; dropped when the statement is very short.
_HALLUCINATIONS = {"thank you.", "thanks for watching!", "thank you for watching.", "you", "bye.", "."}


@dataclass(order=True)
class LiveLine:
    t: float  # seconds after the recording started
    who: str = field(compare=False)  # "you" or "them"
    text: str = field(compare=False)

    @property
    def label(self) -> str:
        return "You" if self.who == "you" else "Them"


def format_lines(lines: list[LiveLine], stamps: bool = False) -> str:
    return "\n".join((f"[{clock(ln.t)}] " if stamps else "") + f"{ln.label}: {ln.text}" for ln in lines)


def clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


class Segmenter:
    """Cuts one continuous audio stream into statements wherever the speaker pauses."""

    def __init__(self):
        self._pending = np.zeros(0, np.float32)  # leftover samples short of a full block
        self._pos = 0  # samples consumed so far: this stream's clock
        self._before: list[np.ndarray] = []  # recent quiet blocks, for pre-roll
        self._blocks: list[np.ndarray] = []  # the statement being collected
        self._start = 0
        self._speech = 0  # loud blocks in it
        self._quiet = 0  # quiet blocks in a row at its end
        self._levels: list[float] = []  # recent block levels, for the noise floor

    def feed(self, samples: np.ndarray) -> list[tuple[float, np.ndarray]]:
        """Add audio; returns finished statements as (start seconds, audio)."""
        data = np.concatenate([self._pending, samples]) if len(self._pending) else samples
        usable = len(data) // BLOCK * BLOCK
        self._pending = data[usable:].copy()
        done = []
        for i in range(0, usable, BLOCK):
            done.extend(self._block(data[i:i + BLOCK]))
        return done

    def flush(self) -> list[tuple[float, np.ndarray]]:
        return self._cut() if self._blocks else []

    def _block(self, block: np.ndarray) -> list[tuple[float, np.ndarray]]:
        level = float(np.sqrt(np.mean(np.square(block))))
        self._levels = (self._levels + [level])[-50:]
        floor = sorted(self._levels)[len(self._levels) // 5]  # quietest fifth of the last 5 s
        loud = level > max(0.006, floor * 3)
        here = self._pos
        self._pos += BLOCK
        if not self._blocks:
            if loud:
                self._blocks = self._before + [block]
                self._start = here - BLOCK * len(self._before)
                self._speech, self._quiet = 1, 0
            else:
                self._before = (self._before + [block])[-PREROLL_BLOCKS:]
            return []
        self._blocks.append(block)
        if loud:
            self._speech += 1
            self._quiet = 0
        else:
            self._quiet += 1
        if self._quiet >= PAUSE_BLOCKS or len(self._blocks) >= MAX_BLOCKS:
            return self._cut()
        return []

    def _cut(self) -> list[tuple[float, np.ndarray]]:
        blocks, speech, quiet, start = self._blocks, self._speech, self._quiet, self._start
        self._blocks, self._before, self._speech, self._quiet = [], [], 0, 0
        if speech < MIN_SPEECH_BLOCKS:
            return []
        if quiet > 2:
            blocks = blocks[: len(blocks) - (quiet - 2)]  # keep 0.2 s of the trailing pause
        return [(max(0, start) / SAMPLE_RATE, np.concatenate(blocks))]


class LiveSession:
    """Reads the given sources, splits them into statements and transcribes them in order."""

    def __init__(self, sources: dict, transcribe: Callable[[np.ndarray], str],
                 on_line: Callable[[LiveLine], None], on_error: Callable[[str], None],
                 on_done: Callable[[], None]):
        self.sources = sources  # {"them": AudioListener, "you": AudioListener}
        self._transcribe = transcribe
        self._on_line, self._on_error, self._on_done = on_line, on_error, on_done
        self._lines: list[LiveLine] = []
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._last_error = ""
        self.started_at = time.time()
        self._t0 = time.monotonic()
        self._stopped_after: float | None = None

    @property
    def elapsed(self) -> float:
        return self._stopped_after if self._stopped_after is not None else time.monotonic() - self._t0

    @property
    def pending(self) -> int:
        """Statements heard but not transcribed yet."""
        return self._queue.qsize()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def lines(self) -> list[LiveLine]:
        with self._lock:
            return list(self._lines)

    def start(self) -> None:
        self._cursors = {who: src.position() for who, src in self.sources.items()}
        self._segmenters = {who: Segmenter() for who in self.sources}
        threading.Thread(target=self._capture, name="live-capture", daemon=True).start()
        threading.Thread(target=self._transcriber, name="live-transcribe", daemon=True).start()

    def stop(self) -> None:
        """Stop listening. Statements already heard still get transcribed, then on_done fires."""
        if not self._stop.is_set():
            self._stopped_after = self.elapsed
            self._stop.set()

    def _capture(self) -> None:
        while not self._stop.wait(0.25):
            self._pull()
        self._pull()
        for who, seg in self._segmenters.items():
            for start, audio in seg.flush():
                self._queue.put((who, start, audio))
        self._queue.put(None)

    def _pull(self) -> None:
        for who, src in self.sources.items():
            samples, self._cursors[who] = src.read_since(self._cursors[who])
            for start, audio in self._segmenters[who].feed(samples):
                self._queue.put((who, start, audio))

    def _transcriber(self) -> None:
        while (item := self._queue.get()) is not None:
            who, start, audio = item
            try:
                text = self._transcribe(audio).strip()
            except Exception as e:
                log.exception("Live transcription failed")
                message = str(e) or type(e).__name__
                if message != self._last_error:  # say it once, not for every statement
                    self._last_error = message
                    self._on_error(message)
                continue
            if not text or (text.lower() in _HALLUCINATIONS and len(audio) < 2 * SAMPLE_RATE):
                continue
            line = LiveLine(start, who, text)
            with self._lock:
                bisect.insort(self._lines, line)  # the two sides finish out of order
            self._on_line(line)
        self._on_done()
