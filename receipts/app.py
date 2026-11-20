"""Wires it together: hotkey → audio → transcript → Claude or Grok → popup.

Two ways in: a hotkey grabs the last N seconds, or ⏺ Record keeps transcribing both sides until
stopped and lets you pick recent statements to check.
"""

import copy
import logging
import threading
import time
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import config, updater
from .audio import IS_MAC, AudioListener, has_virtual_device, list_sources
from .hotkeys import Hotkeys
from .live import LiveSession, clock, format_lines
from .llm import first_message, friendly_error, provider_key, reaction_message, stream_reply
from .stt import Transcriber
from .ui import (ActionCard, ChatWindow, HeardCard, LivePickCard, ReplyCard, SettingsDialog, UserBubble,
                 make_icon, pretty_hotkey)

log = logging.getLogger(__name__)

UPDATE_CHECK_MINUTES = 15
LIVE_CONTEXT_LINES = 30  # how much of a live recording the AI sees around the picked statements
_RECORD = "::record"


class UserFacing(Exception):
    """An error whose message is fine to show as-is."""


class Controller(QObject):
    hotkey_fired = Signal(str)
    worker_event = Signal(int, str, object)
    update_checked = Signal(object, str, bool)  # release or None, error, asked by the user
    update_installed = Signal(str)  # error, or "" on success
    live_event = Signal(str, object)  # ("line" | "error" | "done", payload) from a live recording

    def __init__(self, app: QApplication, use_hotkeys: bool = True, after_update: bool = False):
        super().__init__()
        self.app = app
        self.pending_release = None
        self.installing = False
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
        self.live: LiveSession | None = None
        self._mic: AudioListener | None = None

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
        self.window.record_clicked.connect(self.toggle_recording)
        self.live_event.connect(self._on_live_event)
        self._live_timer = QTimer(self, interval=500, timeout=self._update_live_status)
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
        menu.addAction("Start / stop recording", self.toggle_recording)
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
        # Look for updates shortly after starting, then every few minutes in the background.
        self._update_timer = QTimer(self, interval=UPDATE_CHECK_MINUTES * 60 * 1000,
                                    timeout=lambda: self.check_updates(manual=False))
        if self.cfg.get("check_updates", True):
            QTimer.singleShot(4000, lambda: self.check_updates(manual=False))
            self._update_timer.start()

    # --- actions -------------------------------------------------------------------------

    def run_preset(self, preset_id: str) -> None:
        if self.live:
            self.open_picker(preset_id)
            return
        preset = self._preset(preset_id)
        cfg = copy.deepcopy(self.cfg)
        seconds = int(cfg["listen_seconds"])
        audio = self.listener.last(seconds)
        source = self.listener.source_name or "the audio source"

        self.window.reveal()
        job = self._next_job()
        heard = self.window.add(HeardCard(seconds))
        heard.picked.connect(lambda kind, index, preset_id: self.check_target(heard, kind, index, preset_id))
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
            transcript = self.transcriber.transcribe(audio, cfg)
            if cancelled():
                return
            if not transcript.text:
                raise UserFacing("Heard sound but couldn't make out any words. Type the claim below instead.")
            emit("transcript", transcript)
            convo.append({"role": "user",
                          "content": first_message(preset, transcript=transcript.for_ai(), seconds=seconds)})
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

    def react(self, convo: list, preset_id: str, own_claim: bool = False) -> None:
        """Run another mode on a clip that was already transcribed, continuing its conversation."""
        if self.busy or not convo:
            return
        preset = self._preset(preset_id)
        cfg = copy.deepcopy(self.cfg)
        job = self._next_job()
        self.convo = convo
        convo.append({"role": "user", "content": reaction_message(preset)})
        card = self._reply_card(preset, convo, own_claim=own_claim)
        self.cards = {job: (None, card)}
        self._start(job, lambda emit, cancelled: stream_reply(cfg, convo, emit, cancelled), cfg)

    def check_target(self, heard: HeardCard, kind: str, index: int, preset_id: str) -> None:
        """Check one tapped line, or one speaker, from a clip. Starts a fresh conversation."""
        transcript = heard.transcript
        if transcript is None:
            return
        preset = self._preset(preset_id)
        if kind == "speaker":
            focus = f"Focus only on what S{index} said; treat everyone else as context."
            target, label = f"only what S{index} said", f"S{index}"
        else:
            line = transcript.lines[index]
            who = f" (said by S{line.speaker})" if line.speaker else ""
            focus = f'Focus only on this line{who}: "{line.text}"'
            target, label = f"“{line.text}”", f"S{line.speaker}" if line.speaker else "one line"
        cfg = copy.deepcopy(self.cfg)
        job = self._next_job()  # tapping interrupts whatever was running
        self.window.add(UserBubble(f"{preset['name']} → {target}"))
        self.convo = [{"role": "user", "content": first_message(
            preset, transcript=transcript.for_ai(), seconds=heard.seconds, focus=focus)}]
        convo = self.convo
        card = self._reply_card(preset, convo, label)
        self.cards = {job: (None, card)}
        self._start(job, lambda emit, cancelled: stream_reply(cfg, convo, emit, cancelled), cfg)

    # --- live recording ------------------------------------------------------------------

    def toggle_recording(self) -> None:
        if self.live:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        cfg = copy.deepcopy(self.cfg)
        sources = {"them": self.listener}
        if cfg.get("mic_source") != "none":
            self._mic = AudioListener(auto="mic")
            self._mic.start(cfg.get("mic_source", ""))
            sources["you"] = self._mic

        def transcribe(audio):
            return self.transcriber.transcribe(audio, cfg, diarize=False).text

        session = LiveSession(sources, transcribe,
                              on_line=lambda line: self.live_event.emit("line", line),
                              on_error=lambda message: self.live_event.emit("error", message),
                              on_done=lambda: self.live_event.emit("done", session))
        self.live = session
        session.start()
        self.window.set_recording(True, cfg.get("record_hotkey", ""))
        self._update_live_status()
        self._live_timer.start()
        self.window.reveal()
        fact = next((p for p in cfg["presets"] if p["id"] == "factcheck"), cfg["presets"][0])
        key = f" (**{pretty_hotkey(fact['hotkey'])}**)" if fact.get("hotkey") else ""
        who = "you and them" if "you" in sources else "them (your mic is off in ⚙ Settings)"
        self.window.add_info(f"⏺ **Recording.** Transcribing {who} until you press Stop. Any time, press "
                             f"{fact['name']}{key} to pick recent statements to check.")

    def stop_recording(self) -> None:
        live, self.live = self.live, None
        if not live:
            return
        live.stop()  # statements already heard still get transcribed; "done" arrives after
        if self._mic:  # what it already heard stays buffered for that last pass
            self._mic.stop()
            self._mic = None
        self._live_timer.stop()
        self.window.set_recording(False, self.cfg.get("record_hotkey", ""))
        self.tray.setToolTip(config.APP_NAME)
        self.window.add_info(f"⏹ Stopped after {clock(live.elapsed)}. Finishing the last few statements…")

    def open_picker(self, preset_id: str) -> None:
        lines = self.live.lines()
        self.window.reveal()
        if not lines:
            self.window.add_info("Nothing transcribed yet. Keep talking, then try again.")
            return
        actions = []
        for p in self.cfg["presets"]:
            actions.append((p["id"], p["name"]))
            if p["id"] == "argue":
                actions.append((config.ARGUE_FOR["id"], config.ARGUE_FOR["name"]))
        card = self.window.add(LivePickCard(lines, actions, primary=preset_id, clock=clock))
        card.chosen.connect(lambda mode, picked: self.check_live(lines, picked, mode))

    def check_live(self, lines: list, picked: list, preset_id: str) -> None:
        """Check statements picked from a live recording, with the conversation before them as context."""
        preset = self._preset(preset_id)
        last = max(i for i, line in enumerate(lines) if any(line is p for p in picked))
        context = lines[max(0, last + 1 - LIVE_CONTEXT_LINES): last + 1]
        speakers = {p.who for p in picked}
        own = speakers == {"you"}
        focus = "Focus only on these statement(s):\n" + "\n".join(f'- {p.label}: "{p.text}"' for p in picked)
        if own:
            focus += "\nThese are the user's own words."
        quoted = " / ".join(f"“{p.text}”" for p in picked)
        cfg = copy.deepcopy(self.cfg)
        job = self._next_job()
        self.window.add(UserBubble(f"{preset['name']} → {quoted if len(quoted) < 220 else quoted[:217] + '…'}"))
        seconds = max(1, int(context[-1].t - context[0].t))
        self.convo = [{"role": "user", "content": first_message(
            preset, transcript=format_lines(context), seconds=seconds, focus=focus)}]
        convo = self.convo
        target = "You" if own else "Them" if speakers == {"them"} else ""
        card = self._reply_card(preset, convo, target, own_claim=own)
        self.cards = {job: (None, card)}
        self._start(job, lambda emit, cancelled: stream_reply(cfg, convo, emit, cancelled), cfg)

    def _update_live_status(self) -> None:
        live = self.live
        if not live:
            return
        lines = live.lines()
        title = f"● REC {clock(live.elapsed)} · {len(lines)} statement{'s' if len(lines) != 1 else ''}"
        if live.pending:
            title += f" · {live.pending} transcribing"
        last = f"{lines[-1].label}: {lines[-1].text}" if lines else "Listening…"
        self.window.set_live_status(title, last)
        self.tray.setToolTip(f"{config.APP_NAME} · recording {clock(live.elapsed)}")

    def _on_live_event(self, kind: str, payload) -> None:
        if kind == "error":
            self.window.add_info(f"⚠️ Live transcription: {payload}")
        elif kind == "done":
            session = payload
            lines = session.lines()
            if not lines:
                self.window.add_info("⏹ Recording finished. Nothing was transcribed.")
                return
            self.window.add(ActionCard(
                f"⏹ **Recording finished:** {len(lines)} statements, {clock(session.elapsed)}. "
                "It's only kept until Receipts closes, so save it if you want it.",
                [("Save transcript…", lambda: self._save_transcript(session)),
                 ("Copy transcript", lambda: self._copy_transcript(session))]))

    def _transcript_text(self, session: LiveSession) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(session.started_at))
        return f"Receipts transcript, {stamp}\n\n{format_lines(session.lines(), stamps=True)}\n"

    def _save_transcript(self, session: LiveSession) -> None:
        stamp = time.strftime("%Y-%m-%d %H.%M", time.localtime(session.started_at))
        folder = Path.home() / "Documents"
        path, _ = QFileDialog.getSaveFileName(self.window, "Save transcript",
                                              str((folder if folder.exists() else Path.home())
                                                  / f"Receipts transcript {stamp}.txt"),
                                              "Text files (*.txt)")
        if path:
            Path(path).write_text(self._transcript_text(session), encoding="utf-8")
            self.window.add_info(f"Saved to {path}")

    def _copy_transcript(self, session: LiveSession) -> None:
        QApplication.clipboard().setText(self._transcript_text(session))
        self.window.add_info("Transcript copied.")

    # --- helpers ---------------------------------------------------------------------------

    def _preset(self, preset_id: str) -> dict:
        for built_in in (config.DIG_DEEPER, config.ARGUE_FOR):
            if preset_id == built_in["id"]:
                return built_in
        return next((p for p in self.cfg["presets"] if p["id"] == preset_id), self.cfg["presets"][0])

    def _reaction_options(self, card: ReplyCard) -> list[tuple[str, str]]:
        options = []
        for p in self.cfg["presets"]:
            if p["id"] == card.mode_id:
                continue
            options.append((p["id"], p["name"]))
        if card.mode_id != config.ARGUE_FOR["id"]:
            argue_for = (config.ARGUE_FOR["id"], config.ARGUE_FOR["name"])
            ids = [o[0] for o in options]
            if card.own_claim:  # their own words: backing them up comes first
                options.insert(ids.index("argue") if "argue" in ids else 0, argue_for)
            else:
                options.insert(ids.index("argue") + 1 if "argue" in ids else len(options), argue_for)
        return options + [(config.DIG_DEEPER["id"], config.DIG_DEEPER["name"])]

    def _reply_card(self, preset: dict | None, convo: list, target: str = "",
                    own_claim: bool = False) -> ReplyCard:
        card = self.window.add(ReplyCard(self._tag(preset, target) if preset else None))
        card.mode_id = preset["id"] if preset else None
        card.own_claim = own_claim
        card.reacted.connect(lambda preset_id: self.react(convo, preset_id, card.own_claim))
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
        if self.cfg.get("check_updates", True):
            self._update_timer.start()
        else:
            self._update_timer.stop()
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
        """The blue Update button: one click downloads, swaps in and relaunches."""
        if self.installing:
            return
        if not self.pending_release:
            self.check_updates(manual=True)
            return
        self._start_install(self.pending_release)

    def _on_update_checked(self, release, error: str, manual: bool) -> None:
        if self.installing:
            return
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
        self.installing = True
        self.window.set_updating()
        self.window.reveal()
        self.window.add_info(f"⬇ Updating to version {release.version}. Receipts will reopen by itself.")

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
            self.installing = False
            self.window.add_info(f"⚠️ Update failed: {error}")
            release = self.pending_release
            self.window.show_update(release.version if release else None, release.notes if release else "")
            return
        self.window.add_info("Restarting…")
        updater.relaunch()
        self.quit()

    def _dialog_parent(self):
        return QApplication.activeModalWidget() or self.window

    def quit(self) -> None:
        if self.live:
            self.live.stop()
        if self._mic:
            self._mic.stop()
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
        if self.cfg.get("record_hotkey"):
            bindings[self.cfg["record_hotkey"]] = lambda: self.hotkey_fired.emit(_RECORD)
        try:
            bad = self.hotkeys.set(bindings)
        except Exception as e:
            log.exception("Hotkeys failed")
            self.window.add_info(f"⚠️ Couldn't register hotkeys: {e}")
            return
        if bad:
            self.window.add_info("⚠️ Ignored invalid hotkeys: " + ", ".join(bad))

    def _on_hotkey(self, preset_id: str) -> None:
        if preset_id == _RECORD:
            self.toggle_recording()
        elif preset_id:
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
            heard.set_transcript(payload, self.cfg["presets"])
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
                card.show_reactions(self._reaction_options(card))
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

    def _tag(self, preset: dict, target: str = "") -> str:
        parts = [preset["name"], target, self.cfg.get("ai_provider", "claude").title()]
        return " · ".join(p for p in parts if p)

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
        if self.cfg.get("record_hotkey"):
            lines.append(f"- **{pretty_hotkey(self.cfg['record_hotkey'])}**  ⏺ start / stop a live recording")
        if self.cfg.get("toggle_hotkey"):
            lines.append(f"- **{pretty_hotkey(self.cfg['toggle_hotkey'])}**  show / hide this window")
        if not provider_key(self.cfg):
            lines += ["", "⚠️ **First:** open ⚙ Settings, pick Claude or Grok, and paste that API key."]
        if IS_MAC and not self.cfg["audio_source"] and not has_virtual_device():
            lines += ["", "🍎 **To hear Discord on a Mac**, set up BlackHole once (README → Mac setup). "
                          "Until then, typed claims still work."]
        self.window.add_info("\n".join(lines))
