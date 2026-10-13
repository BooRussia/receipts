"""System-wide hotkeys (they work while a game or Discord has focus)."""

import logging
from typing import Callable

log = logging.getLogger(__name__)


def is_valid(combo: str) -> bool:
    from pynput import keyboard

    try:
        keyboard.HotKey.parse(combo)
        return True
    except ValueError:
        return False


class Hotkeys:
    def __init__(self):
        self._listener = None

    def set(self, bindings: dict[str, Callable[[], None]]) -> list[str]:
        """Replace all hotkeys. Returns the combos that couldn't be parsed."""
        from pynput import keyboard

        self.stop()
        valid, bad = {}, []
        for combo, callback in bindings.items():
            combo = combo.strip().lower()
            if not combo:
                continue
            if is_valid(combo):
                valid[combo] = callback
            else:
                bad.append(combo)
        if valid:
            self._listener = keyboard.GlobalHotKeys(valid)
            self._listener.daemon = True
            self._listener.start()
            log.info("Hotkeys active: %s", ", ".join(valid))
        return bad

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
