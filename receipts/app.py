"""Wires it together: hotkey → last N seconds of audio → transcript → Claude → popup."""

import copy
import logging
import threading

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import config, updater
from .audio import AudioListener, list_sources
from .hotkeys import Hotkeys
from .llm import first_message, friendly_error, provider_key, reaction_message, stream_reply
from .stt import Transcriber
from .ui import ChatWindow, HeardCard, ReplyCard, SettingsDialog, UserBubble, make_icon, pretty_hotkey

log = logging.getLogger(__name__)


class UserFacing(Exception):
    """An error whose message is fine to show as-is."""


class Controller(QObject):
    hotkey_fired = Signal(str)
    worker_event = Signal(int, str, object)
    update_checked = Signal(object, str, bool)  # release or None, error, asked by the user
    update_installed = Signal(str)  # error, or "" on success

    def __init__(self, app: QApplication, use_hotkeys: bool = True, after_update: bool = False):
        super().__init__()
        self.app = app
        self.pending_release = None
        self.cfg = config.load()
        if not config.CONFIG_PATH.exists():
            config.save(self.cfg)  # so "Edit prompts…" has a file to open
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(lambda _: QTimer.singleShot(300, self._reload_config))
        self._watch_config()
        self.job = 0  # bumping this cancels whatever is running
        self.cards: dict[int, tuple] = {}
        self.convo: list | None = None  # the conversation typed follow-ups continue
        self.busy = False

        self.listener = AudioListener()
        self.listener.start(self.cfg["audio_source"])
        self.transcriber = Transcriber()
        self._preload_speech_model()

        self.window = ChatWindow(level=lambda: self.listener.level,
                                 source=lambda: (self.listener.source_name, self.listener.error))
        self.window.set_presets(self.cfg["presets"])
        geo = self.cfg["window"]
        if geo.get("x") is None:
            self.window.place_default(geo["w"], geo["h"])
        else:
            self.window.setGeometry(geo["x"], geo["y"], geo["w"], geo["h"])
        self.window.preset_clicked.connect(self.run_preset)
        self.window.message_sent.connect(self.send_text)
        self.window.settings_clicked.connect(self.open_settings)
        self.window.clear_clicked.connect(self.clear)
        self.window.hidden.connect(self._save_geometry)
        self.window.update_clicked.connect(self.install_update)
        self.update_checked.connect(self._on_update_checked)
        self.update_installed.connect(self._on_update_installed)

        self.hotkey_fired.connect(self._on_hotkey)
        self.worker_event.connect(self._on_worker_event)

        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip(config.APP_NAME)
        menu = QMenu()
        menu.addAction("Show window", lambda: self.window.reveal(activate=True))
        for p in self.cfg["presets"]:
            menu.addAction(f"{p['name']} now", lambda pid=p["id"]: self.run_preset(pid))
        menu.addSeparator()
        menu.addAction("Settings…", self.open_settings)
        menu.addAction("Check for updates", lambda: self.check_updates(manual=True))
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: reason == QSystemTrayIcon.Trigger and self.window.toggle())
        self._tray_menu = menu
        self.tray.show()

        self.hotkeys = Hotkeys()
        self.hotkeys_enabled = use_hotkeys
        if use_hotkeys:
            self._apply_hotkeys()
        self._welcome(after_update)
        self.window.reveal()
        if self.cfg.get("check_updates", True):
            QTimer.singleShot(4000, lambda: self.check_updates(manual=False))

    # --- actions -------------------------------------------------------------------------

    def run_preset(self, preset_id: str) -> None:
        preset = self._preset(preset_id)
        cfg = copy.deepcopy(self.cfg)
        seconds = int(cfg["listen_seconds"])
        audio = self.listener.last(seconds)
        source = self.listener.source_name or "the audio source"

        self.window.reveal()
        job = self._next_job()
        heard = self.window.add(HeardCard(seconds))
        convo = self.convo = []  # each clip is its own conversation
        card = self._reply_card(preset, convo)
        self.cards = {job: (heard, card)}

        def work(emit, cancelled):
            if not self.transcriber.has_speech(audio):
                raise UserFacing(f"Didn't hear anything on “{source}” in the last {seconds}s. "
                                 "Check the audio source in ⚙ Settings, or type the claim below.")
            if cfg["stt_provider"] == "local" and not self.transcriber.is_loaded(cfg):
                emit("status", "Loading the speech model (the first run downloads it)")
            else:
                emit("status", "Transcribing")
            text = self.transcriber.transcribe(audio, cfg)
            if cancelled():
                return
            if not text:
                raise UserFacing("Heard sound but couldn't make out any words. Type the claim below instead.")
            emit("transcript", text)
            convo.append({"role": "user", "content": first_message(preset, transcript=text, seconds=seconds)})
            stream_reply(cfg, convo, emit, cancelled)

        self._start(job, work, cfg)

    def send_text(self, text: str) -> None:
        if self.busy:
            return
        cfg = copy.deepcopy(self.cfg)
        job = self._next_job()
        self.window.add(UserBubble(text))
        if not self.convo:  # nothing to follow up on: treat it as a claim to check
            preset = self.cfg["presets"][0]
            self.convo = [{"role": "user", "content": first_message(preset, typed=text)}]
            card = self._reply_card(preset, self.convo)
        else:
            self.convo.append({"role": "user", "content": text})
            card = self._reply_card(None, self.convo)
        self.cards = {job: (None, card)}
        convo = self.convo
        self._start(job, lambda emit, cancelled: stream_reply(cfg, convo, emit, cancelled), cfg)

    def react(self, convo: list, preset_id: str) -> None:
        """Run another mode on a clip that was already transcribed, continuing its conversation."""
        if self.busy or not convo:
            return
        preset = self._preset(preset_id)
        cfg = copy.deepcopy(self.cfg)
        job = self._next_job()
        self.convo = convo
        convo.append({"role": "user", "content": reaction_message(preset)})
        card = self._reply_card(preset, convo)
        self.cards = {job: (None, card)}
        self._start(job, lambda emit, cancelled: stream_reply(cfg, convo, emit, cancelled), cfg)

    def _preset(self, preset_id: str) -> dict:
        if preset_id == config.DIG_DEEPER["id"]:
            return config.DIG_DEEPER
        return next((p for p in self.cfg["presets"] if p["id"] == preset_id), self.cfg["presets"][0])

    def _reply_card(self, preset: dict | None, convo: list) -> ReplyCard:
        card = self.window.add(ReplyCard(self._tag(preset) if preset else None))
        card.mode_id = preset["id"] if preset else None
        card.reacted.connect(lambda preset_id: self.react(convo, preset_id))
        return card

    def clear(self) -> None:
        self._next_job()
        self.convo = None
        self._set_busy(False)
        self.window.clear_feed()

    def open_settings(self) -> None:
        self.window.reveal(activate=True)
        dlg = SettingsDialog(self.cfg, list_sources, self.listener.set_source, lambda: self.listener.level,
                             check_updates=lambda: self.check_updates(manual=True),
                             version=updater.local_version(), parent=self.window)
        if not dlg.exec():
            return
        old = self.cfg
        self.cfg = dlg.cfg
        config.save(self.cfg)
        self.listener.set_source(self.cfg["audio_source"])
        self.window.set_presets(self.cfg["presets"])
        if (old["local_model"], old["local_device"], old["stt_provider"]) != (
                self.cfg["local_model"], self.cfg["local_device"], self.cfg["stt_provider"]):
            self._preload_speech_model()
        if self.hotkeys_enabled:
            self._apply_hotkeys()

    # --- updates -------------------------------------------------------------------------

    def check_updates(self, manual: bool) -> None:
        def run():
            try:
                release, error = updater.check(), ""
            except Exception as e:
                log.warning("Update check failed: %s", e)
                release, error = None, str(e)
            self.update_checked.emit(release, error, manual)

        threading.Thread(target=run, name="update-check", daemon=True).start()

    def install_update(self) -> None:
        release = self.pending_release
        if not release:
            self.check_updates(manual=True)
            return
        answer = QMessageBox.question(self._dialog_parent(), "Update Receipts",
                                      f"Update to version {release.version} and restart?\n\n{release.notes}")
        if answer == QMessageBox.Yes:
            self._start_install(release)

    def _on_update_checked(self, release, error: str, manual: bool) -> None:
        self.pending_release = release
        self.window.show_update(release.version if release else None, release.notes if release else "")
        if not manual:
            return
        parent = self._dialog_parent()
        if error:
            QMessageBox.warning(parent, "Updates", f"Couldn't check for updates:\n\n{error}")
        elif release:
            answer = QMessageBox.question(parent, "Update available",
                                          f"Version {release.version} is available (you have "
                                          f"{updater.local_version()}).\n\n{release.notes}\n\nUpdate and restart now?")
            if answer == QMessageBox.Yes:
                self._start_install(release)
        else:
            QMessageBox.information(parent, "Updates", f"You're on the latest version ({updater.local_version()}).")

    def _start_install(self, release) -> None:
        if reason := updater.blocked_reason():
            QMessageBox.information(self._dialog_parent(), "Updates", reason)
            return
        self.window.reveal()
        self.window.add_info(f"⬇ Downloading version {release.version}…")

        def run():
            try:
                updater.install(release)
                self.update_installed.emit("")
            except Exception as e:
                log.exception("Update failed")
                self.update_installed.emit(str(e) or type(e).__name__)

        threading.Thread(target=run, name="update-install", daemon=True).start()

    def _on_update_installed(self, error: str) -> None:
        if error:
            self.window.add_info(f"⚠️ Update failed: {error}")
            return
        self.window.add_info("Restarting…")
        updater.relaunch()
        self.quit()

    def _dialog_parent(self):
        return QApplication.activeModalWidget() or self.window

    def quit(self) -> None:
        self._save_geometry()
        self.hotkeys.stop()
        self.listener.stop()
        self.tray.hide()
        self.app.quit()

    # --- plumbing ------------------------------------------------------------------------

    def _apply_hotkeys(self) -> None:
        bindings = {p["hotkey"]: (lambda pid=p["id"]: self.hotkey_fired.emit(pid))
                    for p in self.cfg["presets"] if p.get("hotkey")}
        if self.cfg.get("toggle_hotkey"):
            bindings[self.cfg["toggle_hotkey"]] = lambda: self.hotkey_fired.emit("")
        try:
            bad = self.hotkeys.set(bindings)
        except Exception as e:
            log.exception("Hotkeys failed")
            self.window.add_info(f"⚠️ Couldn't register hotkeys: {e}")
            return
        if bad:
            self.window.add_info("⚠️ Ignored invalid hotkeys: " + ", ".join(bad))

    def _on_hotkey(self, preset_id: str) -> None:
        if preset_id:
            self.run_preset(preset_id)
        else:
            self.window.toggle()

    def _next_job(self) -> int:
        for _heard, card in self.cards.values():
            if card.running:  # its answer will never arrive now
                card.cancel()
        self.cards = {}
        self.job += 1
        return self.job

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.window.set_busy(busy)

    def _start(self, job: int, work, cfg: dict) -> None:
        self._set_busy(True)

        def emit(kind, payload=None):
            self.worker_event.emit(job, kind, payload)

        def cancelled():
            return self.job != job

        def run():
            try:
                work(emit, cancelled)
            except UserFacing as e:
                emit("error", str(e))
            except Exception as e:
                log.exception("Job failed")
                emit("error", friendly_error(e, cfg))
            emit("done")

        threading.Thread(target=run, name=f"job-{job}", daemon=True).start()

    def _on_worker_event(self, job: int, kind: str, payload) -> None:
        if job != self.job or job not in self.cards:
            return  # a newer request replaced this one
        heard, card = self.cards[job]
        if kind == "transcript" and heard:
            heard.set_text(payload)
        elif kind == "status":
            card.set_status(payload)
        elif kind == "search":
            card.add_search(payload)
        elif kind == "text":
            card.append_text(payload)
        elif kind == "reset":
            card.reset_text()
        elif kind == "sources":
            card.set_sources(payload)
        elif kind == "notice":
            card.set_notice(payload)
        elif kind == "error":
            card.set_error(payload)
            if heard and heard.text.text() == "Transcribing…":
                heard.set_text("(nothing transcribed)")
        elif kind == "done":
            card.finish()
            if not card.failed:
                options = [(p["id"], p["name"]) for p in self.cfg["presets"] if p["id"] != card.mode_id]
                card.show_reactions(options + [(config.DIG_DEEPER["id"], config.DIG_DEEPER["name"])])
            self._set_busy(False)

    def _preload_speech_model(self) -> None:
        cfg = copy.deepcopy(self.cfg)

        def load():
            try:
                self.transcriber.preload(cfg)
            except Exception:
                log.exception("Couldn't preload the speech model")

        threading.Thread(target=load, name="whisper-load", daemon=True).start()

    def _watch_config(self) -> None:
        path = str(config.CONFIG_PATH)
        if config.CONFIG_PATH.exists() and path not in self._watcher.files():
            self._watcher.addPath(path)  # editors that save by replacing the file drop the watch

    def _reload_config(self) -> None:
        """Pick up hand edits to config.json (prompts, new modes) without a restart."""
        self._watch_config()
        try:
            new = config.load(strict=True)
        except ValueError:
            log.warning("config.json has a syntax error; keeping the current settings until it's fixed")
            return
        new["window"] = self.cfg["window"]
        if new == self.cfg:
            return
        log.info("Reloaded config.json")
        self.cfg = new
        self.listener.set_source(self.cfg["audio_source"])
        self.window.set_presets(self.cfg["presets"])
        if self.hotkeys_enabled:
            self._apply_hotkeys()

    def _save_geometry(self) -> None:
        g = self.window.geometry()
        self.cfg["window"] = {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()}
        config.save(self.cfg)

    def _tag(self, preset: dict) -> str:
        return f"{preset['name']} · {self.cfg.get('ai_provider', 'claude').title()}"

    def _welcome(self, after_update: bool) -> None:
        if after_update:
            notes = updater.local_notes()
            self.window.add_info(f"✅ **Updated to version {updater.local_version()}.** {notes}")
        lines = [f"**Receipts is running.** When someone says something wrong, hit a hotkey and it grabs the "
                 f"last {self.cfg['listen_seconds']}s of the audio source shown up top, transcribes it, and "
                 "checks it against the web.", ""]
        for p in self.cfg["presets"]:
            if p.get("hotkey"):
                lines.append(f"- **{pretty_hotkey(p['hotkey'])}**  {p['name']}")
        if self.cfg.get("toggle_hotkey"):
            lines.append(f"- **{pretty_hotkey(self.cfg['toggle_hotkey'])}**  show / hide this window")
        if not provider_key(self.cfg):
            lines += ["", "⚠️ **First:** open ⚙ Settings, pick Claude or Grok, and paste that API key."]
        self.window.add_info("\n".join(lines))
