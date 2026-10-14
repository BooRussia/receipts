"""The popup chat window, the settings dialog, and the app icon."""

import html
import re
import time
from typing import Callable
from urllib.parse import urlparse

from PySide6.QtCore import QPointF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QCursor, QDesktopServices, QGuiApplication, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizeGrip, QSpinBox, QVBoxLayout, QWidget,
)

from . import config
from .hotkeys import is_valid
from .llm import PROVIDERS

ACCENT = "#f5c542"
BG = "#14151b"

STYLE = f"""
#card {{ background: {BG}; border: 1px solid #2b2d38; border-radius: 14px; }}
#header {{ border-bottom: 1px solid #23252f; }}
#title {{ color: {ACCENT}; font-weight: 700; font-size: 15px; }}
#source {{ color: #8b8fa3; font-size: 11px; }}
QPushButton#icon {{ background: transparent; color: #8b8fa3; border: none; font-size: 15px;
    padding: 3px 7px; border-radius: 6px; }}
QPushButton#icon:hover {{ background: #23252f; color: #e8e9ee; }}
QPushButton#update {{ background: {ACCENT}; color: {BG}; border: none; border-radius: 9px; font-size: 11px;
    font-weight: 700; padding: 3px 9px; }}
QScrollArea, #feed {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2f3240; border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
#heard {{ background: #191b22; border: 1px solid #252834; border-radius: 10px; }}
#tag {{ color: #8b8fa3; font-size: 10px; font-weight: 700; }}
#modeTag {{ color: {ACCENT}; font-size: 10px; font-weight: 700; }}
#heardText {{ color: #a3a7b9; font-size: 12px; }}
#reply {{ background: #1d2029; border: 1px solid #30343f; border-radius: 12px; }}
#status {{ color: #8b8fa3; font-size: 12px; }}
#body {{ color: #eceef3; font-size: 13px; }}
#sources {{ color: #8b8fa3; font-size: 11px; }}
#error {{ color: #ff8080; font-size: 12px; }}
#user {{ background: #2a3350; border-radius: 12px; }}
#userText {{ color: #eceef3; font-size: 13px; }}
#info {{ color: #b4b8c8; font-size: 12px; }}
#footer {{ border-top: 1px solid #23252f; }}
QPushButton#chip {{ background: #1f222c; color: #d5d7e0; border: 1px solid #30343f; border-radius: 12px;
    padding: 4px 11px; font-size: 12px; }}
QPushButton#chip:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton#chip:disabled, QPushButton#react:disabled {{ color: #4b4f5e; border-color: #262833; }}
QPushButton#react {{ background: transparent; color: #b9bccb; border: 1px solid #343846; border-radius: 10px;
    padding: 2px 9px; font-size: 11px; }}
QPushButton#react:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
#hint {{ color: #6f7386; font-size: 10px; font-weight: 600; }}
QLineEdit#input {{ background: #1b1d25; color: #eceef3; border: 1px solid #30343f; border-radius: 10px;
    padding: 8px 10px; font-size: 13px; }}
QLineEdit#input:focus {{ border-color: {ACCENT}; }}
QPushButton#send {{ background: {ACCENT}; color: {BG}; border: none; border-radius: 10px; font-weight: 700;
    padding: 8px 12px; }}
QPushButton#send:disabled {{ background: #2c2f3b; color: #6b6f80; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in [
        (QPalette.Window, "#1a1b22"), (QPalette.WindowText, "#e3e5ec"), (QPalette.Base, "#121318"),
        (QPalette.AlternateBase, "#1d1f27"), (QPalette.Text, "#e3e5ec"), (QPalette.Button, "#252833"),
        (QPalette.ButtonText, "#e3e5ec"), (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, BG),
        (QPalette.ToolTipBase, "#252833"), (QPalette.ToolTipText, "#e3e5ec"), (QPalette.Link, ACCENT),
        (QPalette.PlaceholderText, "#6b6f80"),
    ]:
        pal.setColor(role, QColor(color))
    app.setPalette(pal)


def make_icon() -> QIcon:
    """A little receipt: gold paper, zig-zag bottom, printed lines."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    left, right, top, bottom = 14, 50, 6, 56
    path.moveTo(left, top)
    path.lineTo(right, top)
    path.lineTo(right, bottom)
    teeth, step = 6, (right - left) / 6
    for i in range(teeth):
        x = right - step * (i + 0.5)
        path.lineTo(x, bottom - 5)
        path.lineTo(right - step * (i + 1), bottom)
    path.closeSubpath()
    p.fillPath(path, QColor(ACCENT))
    p.setPen(QPen(QColor(BG), 3.4, Qt.SolidLine, Qt.RoundCap))
    for y, x2 in [(17, 42), (26, 36), (35, 42)]:
        p.drawLine(QPointF(21, y), QPointF(x2, y))
    p.end()
    return QIcon(pm)


def pretty_hotkey(combo: str) -> str:
    parts = [c.strip().strip("<>") for c in combo.split("+") if c.strip()]
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win", "space": "Space"}
    return "+".join(names.get(c, c.upper()) for c in parts)


_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s")


def md_to_html(md: str) -> str:
    """Markdown → rich text, keeping single line breaks and using a narrow list indent."""
    lines = md.split("\n")
    for i, line in enumerate(lines[:-1]):
        nxt = lines[i + 1]
        if line.strip() and nxt.strip() and not _LIST_ITEM.match(line) and not _LIST_ITEM.match(nxt):
            lines[i] = line.rstrip() + "  "  # markdown hard break
    doc = QTextDocument()
    doc.setMarkdown("\n".join(lines))
    out = doc.toHtml()
    out = out[out.index(">", out.index("<body")) + 1: out.rindex("</body>")]  # drop the doc's own font
    out = re.sub(r"-qt-list-indent: (\d+);", lambda m: f"-qt-list-indent: 0; margin-left: {16 * int(m[1])}px;", out)
    return out.replace("<li style=\" margin-top:6px; margin-bottom:6px;", "<li style=\" margin-top:2px; margin-bottom:2px;")


def _label(text: str = "", name: str = "body", fmt=Qt.MarkdownText) -> QLabel:
    lbl = QLabel()
    lbl.setObjectName(name)
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(Qt.TextBrowserInteraction)
    lbl.setOpenExternalLinks(True)
    if fmt == Qt.MarkdownText:
        lbl.setTextFormat(Qt.RichText)
        lbl.setText(md_to_html(text))
    else:
        lbl.setTextFormat(fmt)
        lbl.setText(text)
    return lbl


# --- feed items --------------------------------------------------------------------------


SPEAKER_COLORS = ["#7cc4ff", "#ff9b7a", "#b69cff", "#7ee0a8", "#ff8fc7", "#e6d36f"]


class HeardCard(QFrame):
    """What the app heard. Each line (and each speaker label) can be tapped to check just that."""

    picked = Signal(str, int, str)  # "line" + line index, or "speaker" + speaker number; then a preset id

    def __init__(self, seconds: int):
        super().__init__(objectName="heard")
        self.seconds = seconds
        self.transcript = None
        self._presets: list[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 10)
        lay.setSpacing(4)
        stamp = time.strftime("%I:%M %p").lstrip("0")
        lay.addWidget(_label(f"🎧  HEARD · LAST {seconds}s · {stamp}", "tag", Qt.PlainText))
        self.text = _label("Transcribing…", "heardText", Qt.PlainText)
        self.text.setOpenExternalLinks(False)
        self.text.linkActivated.connect(self._on_link)
        lay.addWidget(self.text)
        self.hint = _label("", "hint", Qt.PlainText)
        self.hint.hide()
        lay.addWidget(self.hint)

    def set_transcript(self, transcript, presets: list[dict]) -> None:
        self.transcript, self._presets = transcript, presets
        turns: list[list] = []  # [speaker, [line links]]
        for i, line in enumerate(transcript.lines):
            link = f'<a href="line:{i}" style="color:#c9ccd9; text-decoration:none">{html.escape(line.text)}</a>'
            if turns and turns[-1][0] == line.speaker:
                turns[-1][1].append(link)
            else:
                turns.append([line.speaker, [link]])
        rows = []
        for speaker, links in turns:
            chip = ""
            if speaker is not None:
                color = SPEAKER_COLORS[(speaker - 1) % len(SPEAKER_COLORS)]
                chip = (f'<a href="speaker:{speaker}" style="color:{color}; text-decoration:none">'
                        f"<b>S{speaker}</b></a>&nbsp;&nbsp;")
            rows.append(f'<p style="margin-top:0; margin-bottom:5px">{chip}{" ".join(links)}</p>')
        self.text.setTextFormat(Qt.RichText)
        self.text.setText("".join(rows))
        self.hint.setText("TAP A LINE" + (" OR A SPEAKER" if transcript.speakers else "") + " TO CHECK JUST THAT")
        self.hint.show()

    def _on_link(self, href: str) -> None:
        kind, _, value = href.partition(":")
        if not value.isdigit() or not self._presets:
            return
        menu = QMenu(self)
        title = menu.addAction(f"Only what S{value} said" if kind == "speaker" else "Just this line")
        title.setEnabled(False)
        for p in self._presets:
            menu.addAction(p["name"], lambda pid=p["id"]: self.picked.emit(kind, int(value), pid))
        menu.exec(QCursor.pos())

    def set_text(self, text: str) -> None:
        self.text.setToolTip(text if len(text) > 420 else "")
        self.text.setText(("…" + text[-420:].lstrip()) if len(text) > 420 else text)


class UserBubble(QWidget):
    def __init__(self, text: str):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(40, 0, 0, 0)
        row.addStretch(1)
        bubble = QFrame(objectName="user")
        lay = QVBoxLayout(bubble)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.addWidget(_label(text, "userText", Qt.PlainText))
        row.addWidget(bubble)


class ReplyCard(QFrame):
    """The AI's answer, streamed in, with a live status line, sources and reaction buttons."""

    reacted = Signal(str)  # preset id to run on the same clip

    def __init__(self, mode: str | None):
        super().__init__(objectName="reply")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 11, 14, 12)
        lay.setSpacing(6)
        if mode:
            lay.addWidget(_label(f"🧾  {mode.upper()}", "modeTag", Qt.PlainText))
        self.status = _label("", "status", Qt.PlainText)
        self.body = _label("", "body")
        self.body.hide()
        self.error = _label("", "error", Qt.PlainText)
        self.error.hide()
        self.checked = _label("", "sources", Qt.PlainText)
        self.checked.hide()
        self.sources = _label("", "sources", Qt.RichText)
        self.sources.hide()
        for w in (self.status, self.body, self.error, self.checked, self.sources):
            lay.addWidget(w)
        self.reactions = QHBoxLayout()
        self.reactions.setSpacing(5)
        lay.addLayout(self.reactions)
        self.mode_id: str | None = None  # which mode produced this answer
        self.failed = False
        self.running = True
        self._text = ""
        self._dirty = False
        self._status = ""
        self._searches: list[str] = []
        self._dots = 0
        self._timer = QTimer(self, interval=70, timeout=self._tick)
        self._timer.start()

    def set_status(self, text: str) -> None:
        self._status = text
        self._render_status()

    def add_search(self, query: str) -> None:
        self._searches.append(query)
        self._status = f"Searching: {query}"
        self._render_status()

    def append_text(self, delta: str) -> None:
        self._text += delta
        self._dirty = True

    def reset_text(self) -> None:
        self._text, self._dirty = "", True

    def set_sources(self, sources: list) -> None:
        if not sources:
            return
        lines = []
        for url, title in sources:
            parsed = urlparse(url)
            domain = parsed.netloc.removeprefix("www.")
            if not title or title.strip("[]").isdigit():  # Grok titles are often just "1", "2"
                title = parsed.path.strip("/").replace("-", " ").replace("_", " ").split("/")[-1]
            title = title if len(title) <= 52 else title[:50] + "…"
            lines.append(f'<a href="{html.escape(url)}" style="color:{ACCENT}; text-decoration:none">'
                         f"{html.escape(domain)}</a>&nbsp;&nbsp;{html.escape(title)}")
        self.sources.setText("<b>Sources</b><br>" + "<br>".join(lines))
        self.sources.show()

    def set_notice(self, text: str) -> None:
        self.append_text(f"\n\n_{text}_")

    def set_error(self, text: str) -> None:
        self.failed = True
        self.error.setText(text)
        self.error.show()

    def cancel(self) -> None:
        self.failed = True
        self.finish()
        self.checked.setText("Stopped: a newer request replaced this one.")
        self.checked.show()

    def show_reactions(self, options: list[tuple[str, str]]) -> None:
        """Buttons that re-run another mode on this same clip, e.g. [("roast", "Roast")]."""
        self.reactions.addWidget(_label("SAME CLIP", "hint", Qt.PlainText))
        for preset_id, name in options:
            b = QPushButton(name, objectName="react", cursor=Qt.PointingHandCursor)
            b.setToolTip(f"{name} what was already heard, without grabbing new audio")
            b.clicked.connect(lambda _=False, pid=preset_id: self.reacted.emit(pid))
            self.reactions.addWidget(b)
        self.reactions.addStretch(1)

    def finish(self) -> None:
        self.running = False
        self._status = ""
        self._tick()
        self._timer.stop()
        if self._searches:
            self.checked.setText("🔎  " + " · ".join(self._searches))
            self.checked.show()

    def _render_status(self) -> None:
        if self._status:
            self.status.setText("◐◓◑◒"[self._dots // 2 % 4] + "  " + self._status.rstrip(".…"))
            self.status.show()
        else:
            self.status.hide()

    def _tick(self) -> None:
        self._dots += 1
        self._render_status()
        if self._dirty:
            self._dirty = False
            self.body.setText(md_to_html(self._text))
            self.body.setVisible(bool(self._text.strip()))


# --- main window -------------------------------------------------------------------------


class _Header(QFrame):
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.window().windowHandle():
            self.window().windowHandle().startSystemMove()


class ChatWindow(QWidget):
    preset_clicked = Signal(str)
    message_sent = Signal(str)
    settings_clicked = Signal()
    clear_clicked = Signal()
    update_clicked = Signal()
    hidden = Signal()

    def __init__(self, level: Callable[[], float], source: Callable[[], tuple[str, str]]):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle(config.APP_NAME)
        self.setWindowIcon(make_icon())
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(STYLE)
        self.setMinimumSize(340, 360)
        self._level, self._source = level, source

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame(objectName="card")
        outer.addWidget(card)
        col = QVBoxLayout(card)
        col.setContentsMargins(1, 1, 1, 1)
        col.setSpacing(0)

        header = _Header(objectName="header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 9, 8, 9)
        hl.setSpacing(6)
        icon = QLabel()
        icon.setPixmap(make_icon().pixmap(18, 18))
        hl.addWidget(icon)
        hl.addWidget(_label("Receipts", "title", Qt.PlainText))
        self.dot = QLabel("●")
        self.dot.setFixedWidth(14)
        hl.addWidget(self.dot)
        self.source_lbl = _label("", "source", Qt.PlainText)
        self.source_lbl.setWordWrap(False)
        self.source_lbl.setMinimumWidth(40)
        hl.addWidget(self.source_lbl, 1)
        self.update_btn = QPushButton("⬆ Update", objectName="update", cursor=Qt.PointingHandCursor)
        self.update_btn.clicked.connect(self.update_clicked.emit)
        self.update_btn.hide()
        hl.addWidget(self.update_btn)
        for text, tip, sig in [("⚙", "Settings", self.settings_clicked), ("⌫", "Clear", self.clear_clicked)]:
            b = QPushButton(text, objectName="icon", toolTip=tip, cursor=Qt.PointingHandCursor)
            b.clicked.connect(sig.emit)
            hl.addWidget(b)
        close = QPushButton("✕", objectName="icon", toolTip="Hide (Esc)", cursor=Qt.PointingHandCursor)
        close.clicked.connect(self.hide)
        hl.addWidget(close)
        col.addWidget(header)

        self.scroll = QScrollArea(widgetResizable=True, frameShape=QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.feed = QWidget(objectName="feed")
        self.feed_lay = QVBoxLayout(self.feed)
        self.feed_lay.setContentsMargins(12, 12, 12, 12)
        self.feed_lay.setSpacing(10)
        self.feed_lay.addStretch(1)
        self.scroll.setWidget(self.feed)
        bar = self.scroll.verticalScrollBar()
        self._stick = True
        bar.valueChanged.connect(lambda v: setattr(self, "_stick", v >= bar.maximum() - 40))
        bar.rangeChanged.connect(lambda _lo, hi: self._stick and bar.setValue(hi))
        col.addWidget(self.scroll, 1)

        footer = QFrame(objectName="footer")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(12, 10, 12, 4)
        fl.setSpacing(8)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        fl.addLayout(self.chips)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.input = QLineEdit(objectName="input", placeholderText="Type a claim or ask a follow-up…")
        self.input.returnPressed.connect(self._send)
        self.send = QPushButton("➤", objectName="send", cursor=Qt.PointingHandCursor)
        self.send.clicked.connect(self._send)
        row.addWidget(self.input, 1)
        row.addWidget(self.send)
        fl.addLayout(row)
        grip_row = QHBoxLayout()
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(self))
        fl.addLayout(grip_row)
        col.addWidget(footer)

        self._meter = QTimer(self, interval=120, timeout=self._update_meter)
        self._meter.start()

    # public API used by the controller

    def set_presets(self, presets: list[dict]) -> None:
        while self.chips.count():
            item = self.chips.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.chips.addWidget(_label("NEW CLIP", "hint", Qt.PlainText))
        for p in presets:
            b = QPushButton(p["name"], objectName="chip", cursor=Qt.PointingHandCursor)
            hk = p.get("hotkey", "")
            b.setToolTip(f"Grab the latest audio and {p['name'].lower()}" + (f"  ({pretty_hotkey(hk)})" if hk else ""))
            b.clicked.connect(lambda _=False, pid=p["id"]: self.preset_clicked.emit(pid))
            self.chips.addWidget(b)
        self.chips.addStretch(1)

    def reveal(self, activate: bool = False) -> None:
        self.show()
        self.raise_()
        if activate:
            self.activateWindow()
            self.input.setFocus()

    def toggle(self) -> None:
        self.hide() if self.isVisible() else self.reveal(activate=True)

    def show_update(self, version: str | None, notes: str = "") -> None:
        self.update_btn.setVisible(bool(version))
        if version:
            self.update_btn.setToolTip(f"Version {version} is available. {notes}".strip())

    def set_busy(self, busy: bool) -> None:
        self.send.setEnabled(not busy)
        for b in self.feed.findChildren(QPushButton, "react"):
            b.setEnabled(not busy)
        self.input.setPlaceholderText("Working on it…" if busy else "Type a claim or ask a follow-up…")

    def add(self, widget: QWidget) -> QWidget:
        self._stick = True
        self.feed_lay.insertWidget(self.feed_lay.count() - 1, widget)
        return widget

    def add_info(self, markdown: str) -> QLabel:
        return self.add(_label(markdown, "info"))

    def clear_feed(self) -> None:
        while self.feed_lay.count() > 1:
            w = self.feed_lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def place_default(self, w: int, h: int) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(w, h)
        self.move(screen.right() - w - 24, screen.top() + 80)

    # internals

    def _send(self) -> None:
        text = self.input.text().strip()
        if text and self.send.isEnabled():
            self.input.clear()
            self.message_sent.emit(text)

    def _update_meter(self) -> None:
        name, error = self._source()
        level = self._level()
        if error:
            color, label = "#ff6b6b", f"Audio error: {error}"
        elif not name:
            color, label = "#555a6b", "Starting audio…"
        else:
            color = "#4ade80" if level > 0.01 else ("#2f7a4b" if level > 0.002 else "#555a6b")
            label = name
        self.dot.setStyleSheet(f"color: {color}; font-size: 11px;")
        metrics = self.source_lbl.fontMetrics()
        self.source_lbl.setText(metrics.elidedText(label, Qt.ElideRight, max(40, self.source_lbl.width())))
        self.source_lbl.setToolTip(label)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)

    def hideEvent(self, e):
        self.hidden.emit()
        super().hideEvent(e)


# --- settings ----------------------------------------------------------------------------


class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, list_sources: Callable[[], list], preview_source: Callable[[str], None],
                 level: Callable[[], float], check_updates: Callable[[], None], version: str, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Receipts settings")
        self.setWindowIcon(make_icon())
        self.setMinimumWidth(520)
        self.cfg = {**cfg, "presets": [dict(p) for p in cfg["presets"]]}
        self._list_sources, self._preview, self._level = list_sources, preview_source, level
        self._original_source = cfg["audio_source"]

        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(12)

        # Audio
        audio = QGroupBox("What to listen to")
        af = QFormLayout(audio)
        src_row = QHBoxLayout()
        self.source = QComboBox()
        self.source.setMinimumContentsLength(28)
        self.source.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        refresh = QPushButton("↻", toolTip="Rescan audio devices")
        refresh.setFixedWidth(32)
        refresh.clicked.connect(self._fill_sources)
        src_row.addWidget(self.source, 1)
        src_row.addWidget(refresh)
        af.addRow("Audio source", src_row)
        self.meter = QProgressBar(maximum=100, textVisible=False)
        self.meter.setFixedHeight(8)
        af.addRow("Level", self.meter)
        hint = QLabel("Pick the output Discord plays into. With SteelSeries Sonar that's “SteelSeries Sonar - Chat”. "
                      "Have someone talk in Discord: the bar should bounce.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b8fa3; font-size:11px;")
        af.addRow(hint)
        self.seconds = QSpinBox(minimum=5, maximum=170, suffix=" seconds")
        self.seconds.setValue(int(cfg["listen_seconds"]))
        af.addRow("Grab the last", self.seconds)
        lay.addWidget(audio)
        self._fill_sources()
        self.source.currentIndexChanged.connect(lambda _: self._preview(self.source.currentData()))

        # Transcription
        stt = QGroupBox("Speech to text")
        self.sf = QFormLayout(stt)
        self.provider = QComboBox()
        for key, label in [("local", "On this PC (free, private)"),
                           ("xai", "xAI API (labels who's talking)"),
                           ("openai", "OpenAI API"), ("groq", "Groq API (very fast, has a free tier)")]:
            self.provider.addItem(label, key)
        self.provider.setCurrentIndex(max(0, self.provider.findData(cfg["stt_provider"])))
        self.sf.addRow("Transcribe with", self.provider)
        self.local_model = QComboBox(editable=True)
        self.local_model.addItems(["base.en", "small.en", "medium.en", "large-v3-turbo"])
        self.local_model.setCurrentText(cfg["local_model"])
        self.sf.addRow("Whisper model", self.local_model)
        self.local_device = QComboBox()
        self.local_device.addItem("CPU", "cpu")
        self.local_device.addItem("NVIDIA GPU (CUDA)", "cuda")
        self.local_device.setCurrentIndex(max(0, self.local_device.findData(cfg["local_device"])))
        self.sf.addRow("Run on", self.local_device)
        self.openai_key = self._secret(cfg["openai_api_key"])
        self.sf.addRow("OpenAI API key", self.openai_key)
        self.groq_key = self._secret(cfg["groq_api_key"])
        self.sf.addRow("Groq API key", self.groq_key)
        self.xai_stt_note = QLabel("Uses the xAI API key from the AI section. Shows S1, S2, … for each voice.")
        self.xai_stt_note.setWordWrap(True)
        self.xai_stt_note.setStyleSheet("color:#8b8fa3; font-size:11px;")
        self.sf.addRow(self.xai_stt_note)
        self.provider.currentIndexChanged.connect(self._sync_stt_rows)
        self._sync_stt_rows()
        lay.addWidget(stt)

        # AI: Claude or Grok
        ai = QGroupBox("AI that does the fact-checking")
        self.af = cf = QFormLayout(ai)
        self.ai_provider = QComboBox()
        for key, label in PROVIDERS.items():
            self.ai_provider.addItem(label, key)
        self.ai_provider.setCurrentIndex(max(0, self.ai_provider.findData(cfg["ai_provider"])))
        cf.addRow("Use", self.ai_provider)
        self.anthropic_key = self._secret(cfg["anthropic_api_key"], "sk-ant-…  (from console.anthropic.com)")
        cf.addRow("Anthropic API key", self.anthropic_key)
        self.model = QComboBox(editable=True)
        self.model.addItems(["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
        self.model.setCurrentText(cfg["claude_model"])
        cf.addRow("Claude model", self.model)
        self.effort = QComboBox()
        for key, label in [("low", "Low (fastest)"), ("medium", "Medium"), ("high", "High (most thorough)")]:
            self.effort.addItem(label, key)
        self.effort.setCurrentIndex(max(0, self.effort.findData(cfg["effort"])))
        cf.addRow("Effort", self.effort)
        self.xai_key = self._secret(cfg["xai_api_key"], "xai-…  (from console.x.ai)")
        cf.addRow("xAI API key", self.xai_key)
        self.grok_model = QComboBox(editable=True)
        self.grok_model.addItems(["grok-4.7", "grok-4.3", "grok-4.20-0309-non-reasoning"])
        self.grok_model.setCurrentText(cfg["grok_model"])
        cf.addRow("Grok model", self.grok_model)
        self.ai_provider.currentIndexChanged.connect(self._sync_ai_rows)
        self.provider.currentIndexChanged.connect(self._sync_ai_rows)
        self._sync_ai_rows()
        lay.insertWidget(0, ai)

        # Hotkeys
        hk = QGroupBox("Hotkeys")
        hf = QFormLayout(hk)
        self.hotkey_edits = []
        for p in self.cfg["presets"]:
            edit = QLineEdit(p.get("hotkey", ""))
            self.hotkey_edits.append(edit)
            hf.addRow(p["name"], edit)
        self.toggle_edit = QLineEdit(cfg["toggle_hotkey"])
        hf.addRow("Show / hide window", self.toggle_edit)
        tip = QLabel("Format: <ctrl>+<alt>+f or <ctrl>+<shift>+<f9>. On a Stream Deck, add a Hotkey "
                     "action that presses the same keys.")
        tip.setTextFormat(Qt.PlainText)
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8b8fa3; font-size:11px;")
        hf.addRow(tip)
        lay.addWidget(hk)

        # Updates
        up = QGroupBox("Updates")
        ul = QHBoxLayout(up)
        ul.addWidget(QLabel(f"Version {version}"))
        self.auto_update = QCheckBox("Check when Receipts starts")
        self.auto_update.setChecked(bool(cfg.get("check_updates", True)))
        ul.addWidget(self.auto_update, 1)
        check = QPushButton("Check for updates")
        check.clicked.connect(check_updates)
        ul.addWidget(check)
        lay.addWidget(up)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        prompts = buttons.addButton("Edit prompts…", QDialogButtonBox.ResetRole)
        prompts.setToolTip("Opens config.json, where each mode's prompt lives")
        prompts.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.CONFIG_PATH))))
        logs = buttons.addButton("Open log", QDialogButtonBox.ResetRole)
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.LOG_PATH))))
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        scroll = QScrollArea(widgetResizable=True, frameShape=QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll, 1)
        outer.addWidget(buttons)
        avail = (parent.screen() if parent else QGuiApplication.primaryScreen()).availableGeometry()
        self.resize(560, min(content.sizeHint().height() + 70, int(avail.height() * 0.9)))

        self._timer = QTimer(self, interval=80, timeout=lambda: self.meter.setValue(min(100, int(self._level() * 600))))
        self._timer.start()

    def _secret(self, value: str, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit(value, placeholderText=placeholder)
        edit.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        return edit

    def _fill_sources(self) -> None:
        current = self.source.currentData() if self.source.count() else self.cfg["audio_source"]
        self.source.blockSignals(True)
        self.source.clear()
        self.source.addItem("Auto (Sonar Chat if found, else everything you hear)", "")
        try:
            for key, label in self._list_sources():
                self.source.addItem(label, key)
        except Exception as e:  # device enumeration can fail on odd drivers; keep the dialog usable
            self.source.addItem(f"Couldn't list devices: {e}", current)
        idx = self.source.findData(current)
        if idx < 0 and current:
            self.source.addItem(f"{current.split(':', 1)[-1]} (not connected)", current)
            idx = self.source.count() - 1
        self.source.setCurrentIndex(max(0, idx))
        self.source.blockSignals(False)

    def _sync_ai_rows(self) -> None:
        grok = self.ai_provider.currentData() == "grok"
        for w in (self.anthropic_key, self.model, self.effort):
            self.af.setRowVisible(w, not grok)
        self.af.setRowVisible(self.grok_model, grok)
        # The xAI key also powers xAI speech-to-text, so show it for either.
        self.af.setRowVisible(self.xai_key, grok or self.provider.currentData() == "xai")

    def _sync_stt_rows(self) -> None:
        provider = self.provider.currentData()
        self.sf.setRowVisible(self.local_model, provider == "local")
        self.sf.setRowVisible(self.local_device, provider == "local")
        self.sf.setRowVisible(self.openai_key, provider == "openai")
        self.sf.setRowVisible(self.groq_key, provider == "groq")
        self.sf.setRowVisible(self.xai_stt_note, provider == "xai")

    def _save(self) -> None:
        combos = [e.text().strip().lower() for e in self.hotkey_edits] + [self.toggle_edit.text().strip().lower()]
        bad = [c for c in combos if c and not is_valid(c)]
        if bad:
            QMessageBox.warning(self, "Hotkeys", "These hotkeys aren't valid:\n\n" + "\n".join(bad)
                                + "\n\nUse the form <ctrl>+<alt>+f")
            return
        for p, combo in zip(self.cfg["presets"], combos):
            p["hotkey"] = combo
        self.cfg.update(
            audio_source=self.source.currentData(),
            listen_seconds=self.seconds.value(),
            stt_provider=self.provider.currentData(),
            local_model=self.local_model.currentText().strip(),
            local_device=self.local_device.currentData(),
            openai_api_key=self.openai_key.text().strip(),
            groq_api_key=self.groq_key.text().strip(),
            ai_provider=self.ai_provider.currentData(),
            anthropic_api_key=self.anthropic_key.text().strip(),
            claude_model=self.model.currentText().strip(),
            effort=self.effort.currentData(),
            xai_api_key=self.xai_key.text().strip(),
            grok_model=self.grok_model.currentText().strip(),
            check_updates=self.auto_update.isChecked(),
            toggle_hotkey=combos[-1],
        )
        self.accept()

    def reject(self) -> None:
        self._preview(self._original_source)
        super().reject()

