"""System-wide hotkeys (they work while a game or Discord has focus).

Windows uses pynput. macOS uses Carbon's RegisterEventHotKey instead: it needs no Accessibility
permission and is what most Mac apps use for global shortcuts.
"""

import logging
import sys
from typing import Callable

log = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin"


def is_valid(combo: str) -> bool:
    if IS_MAC:
        return _mac_parse(combo) is not None
    from pynput import keyboard

    try:
        keyboard.HotKey.parse(combo)
        return True
    except ValueError:
        return False


class Hotkeys:
    def __init__(self):
        self._listener = None
        self._mac = _MacHotkeys() if IS_MAC else None

    def set(self, bindings: dict[str, Callable[[], None]]) -> list[str]:
        """Replace all hotkeys. Returns the combos that couldn't be used."""
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
        if self._mac:
            bad += self._mac.set(valid)
        elif valid:
            from pynput import keyboard

            self._listener = keyboard.GlobalHotKeys(valid)
            self._listener.daemon = True
            self._listener.start()
        log.info("Hotkeys active: %s", ", ".join(c for c in valid if c not in bad))
        return bad

    def stop(self) -> None:
        if self._mac:
            self._mac.clear()
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


# --- macOS ----------------------------------------------------------------------------------

_MAC_MODIFIERS = {"cmd": 1 << 8, "shift": 1 << 9, "alt": 1 << 11, "ctrl": 1 << 12}
# US-layout virtual key codes (Carbon kVK_*). Letters are mapped by position, like the Windows side.
_MAC_KEYS = {
    **dict(zip("asdfhgzxcv", range(0x00, 0x0A))), "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F,
    "y": 0x10, "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "=": 0x18,
    "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D, "]": 0x1E, "o": 0x1F, "u": 0x20, "[": 0x21,
    "i": 0x22, "p": 0x23, "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29, "\\": 0x2A, ",": 0x2B,
    "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "`": 0x32, "space": 0x31,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64,
    "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F, "f13": 0x69, "f14": 0x6B, "f15": 0x71,
    "f16": 0x6A, "f17": 0x40, "f18": 0x4F, "f19": 0x50, "f20": 0x5A,
}

_MAC_FKEYS = {code for name, code in _MAC_KEYS.items() if name[0] == "f" and name[1:].isdigit()}


def _mac_parse(combo: str) -> tuple[int, int] | None:
    """"<ctrl>+<alt>+f" -> (key code, Carbon modifier mask), or None if it can't be a hotkey."""
    mods, key = 0, None
    for part in (p.strip().lower() for p in combo.split("+")):
        name = part.strip("<>")
        if name in ("cmd", "command", "super"):
            name = "cmd"
        if name in ("option",):
            name = "alt"
        if name in _MAC_MODIFIERS and part.startswith("<"):
            mods |= _MAC_MODIFIERS[name]
        elif key is None and name in _MAC_KEYS:
            key = _MAC_KEYS[name]
        else:
            return None
    if key is None or not (mods or key in _MAC_FKEYS):  # plain letters would steal normal typing
        return None
    return key, mods


class _MacHotkeys:
    _SIGNATURE = 0x52435054  # 'RCPT'

    def __init__(self):
        import ctypes

        self._ct = ctypes
        carbon = self._carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")

        class HotKeyID(ctypes.Structure):
            _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

        class EventTypeSpec(ctypes.Structure):
            _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

        self._HotKeyID = HotKeyID
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, HotKeyID, ctypes.c_void_p,
                                               ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        carbon.RegisterEventHotKey.restype = ctypes.c_int32
        carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        carbon.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                             ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
        carbon.GetEventParameter.restype = ctypes.c_int32
        handler_type = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, handler_type, ctypes.c_ulong,
                                               ctypes.POINTER(EventTypeSpec), ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_void_p)]
        carbon.InstallEventHandler.restype = ctypes.c_int32

        self._callbacks: dict[int, Callable[[], None]] = {}
        self._refs: list = []
        self._handler = handler_type(self._on_event)  # keep a reference or it gets collected
        spec = EventTypeSpec(0x6B657962, 5)  # kEventClassKeyboard 'keyb', kEventHotKeyPressed
        status = carbon.InstallEventHandler(carbon.GetApplicationEventTarget(), self._handler, 1,
                                            ctypes.byref(spec), None, None)
        if status:
            raise RuntimeError(f"Couldn't listen for hotkeys (Carbon error {status})")

    def set(self, bindings: dict[str, Callable[[], None]]) -> list[str]:
        self.clear()
        ct, bad = self._ct, []
        for i, (combo, callback) in enumerate(bindings.items(), start=1):
            key, mods = _mac_parse(combo)
            ref = ct.c_void_p()
            status = self._carbon.RegisterEventHotKey(key, mods, self._HotKeyID(self._SIGNATURE, i),
                                                      self._carbon.GetApplicationEventTarget(), 0, ct.byref(ref))
            if status:  # usually -9878: another app already owns this combination
                log.warning("Hotkey %s unavailable (Carbon error %s)", combo, status)
                bad.append(combo)
                continue
            self._refs.append(ref)
            self._callbacks[i] = callback
        return bad

    def clear(self) -> None:
        for ref in self._refs:
            self._carbon.UnregisterEventHotKey(ref)
        self._refs, self._callbacks = [], {}

    def _on_event(self, _call_ref, event, _user_data) -> int:
        hotkey = self._HotKeyID()
        status = self._carbon.GetEventParameter(event, 0x2D2D2D2D, 0x686B6964, None,  # '----', 'hkid'
                                                self._ct.sizeof(hotkey), None, self._ct.byref(hotkey))
        callback = self._callbacks.get(hotkey.id) if status == 0 else None
        if callback:
            try:
                callback()
            except Exception:
                log.exception("Hotkey handler failed")
        return 0
