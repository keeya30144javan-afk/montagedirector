"""The pop-up windows: settings, the shot picker, instruction box, text cards and scene keys."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..director_ai import MODELS
from ..paths import APP_NAME
from . import store
from .common import _mmss, _parse_time
from .widgets import TipBubble

log = logging.getLogger(__name__)

class SettingsDialog(QDialog):
    def __init__(self, parent, model: str):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        form = QFormLayout(self)
        form.setSpacing(10)
        intro = QLabel("The AI Director uses your own Anthropic API key. You pay Anthropic directly "
                       "for what you use; the cost of each edit is shown after it's made.")
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        form.addRow(intro)
        self.key = QLineEdit(store.get_api_key())
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("sk-ant-...")
        form.addRow("API key", self.key)
        link = QLabel('<a style="color:#a38bff" href="https://console.anthropic.com/settings/keys">'
                      "Get an API key at console.anthropic.com</a>")
        link.setOpenExternalLinks(True)
        form.addRow("", link)
        self.model = QComboBox()
        for mid, (label, *_rest) in MODELS.items():
            self.model.addItem(label, mid)
        self.model.setCurrentIndex(max(0, self.model.findData(model)))
        form.addRow("AI model", self.model)
        self.where = QLabel("")
        self.where.setObjectName("Muted")
        form.addRow("", self.where)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self):
        where = store.set_api_key(self.key.text())
        log.info("API key saved to %s", where)
        super().accept()


class ShotPicker(QDialog):
    """Your shots as thumbnails. Pick one to hold, Ctrl+click several for the cuts in a part, or just
    browse: search the AI descriptions, and right-click to mark ★ favourites or ✕ never use."""

    def __init__(self, parent, project, seconds: float | None = None):
        super().__init__(parent)
        self.project = project
        self.footage = project.footage
        self.tags = project.shot_tags or {}
        self.browse = seconds is None
        self.setWindowTitle("Your shots" if self.browse else "Pick a shot")
        self.resize(1000, 680)
        self.chosen = None
        self.marks_changed = False
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        if self.browse:
            msg = "Right-click a shot to mark it ★ favourite (used more, at the big moments) or ✕ never use."
        else:
            msg = (f"Click one shot to hold it for this {seconds:.1f} s part, or Ctrl+click several to use them in "
                   "that order on the cuts here. Right-click to mark ★ / ✕.")
        lab = QLabel(msg)
        lab.setWordWrap(True)
        top.addWidget(lab, 1)
        v.addLayout(top)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search what's in the shots (needs Describe my shots), e.g. \"nappa\", "
                                       "\"team\", \"explosion\"" if not self.tags else
                                       "Search what's in the shots, e.g. \"nappa\", \"team\", \"explosion\"")
        self.search.textChanged.connect(self._fill)
        row.addWidget(self.search, 1)
        row.addWidget(QLabel("Show:"))
        self.order = QComboBox()
        for key, label in (("time", "In footage order"), ("people", "Most characters first"),
                           ("calm", "Calmest first"), ("action", "Most action first"), ("long", "Longest first"),
                           ("marked", "★ favourites first")):
            self.order.addItem(label, key)
        self.order.currentIndexChanged.connect(self._fill)
        row.addWidget(self.order)
        v.addLayout(row)
        self.grid = QListWidget()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setIconSize(QSize(208, 117))
        self.grid.setGridSize(QSize(224, 156))
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QListWidget.SingleSelection if self.browse else QListWidget.ExtendedSelection)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._mark_menu)
        if not self.browse:
            self.grid.itemDoubleClicked.connect(lambda _: self.accept())
        self.picked: list[int] = []  # in the order they were clicked
        self.grid.itemSelectionChanged.connect(self._selection_changed)
        v.addWidget(self.grid, 1)
        self.count = QLabel("")
        self.count.setObjectName("Muted")
        v.addWidget(self.count)
        self.freeze = QCheckBox("Freeze on this frame (a still picture instead of the moving clip)")
        self.freeze.setChecked(bool(seconds and seconds > 4.0))
        self.freeze.setVisible(not self.browse)
        v.addWidget(self.freeze)
        if self.browse:
            buttons = QDialogButtonBox(QDialogButtonBox.Close)
            buttons.rejected.connect(self.reject)
            self.ok = QPushButton()
        else:
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            self.ok = buttons.button(QDialogButtonBox.Ok)
            self.ok.setText("Use this shot")
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self._fill()

    def _mark(self, shot_id: int) -> str:
        key = self.project.shot_key(shot_id)
        return "★ " if key in self.project.favorites else ("✕ " if key in self.project.banned else "")

    def _fill(self):
        shots = [s for s in self.footage.shots if s.usable]
        q = self.search.text().strip().lower()
        if q:
            words = q.split()
            shots = [s for s in shots if all(w in (self.tags.get(s.id, {}).get("desc", "") + " " +
                                                   self.tags.get(s.id, {}).get("kind", "")).lower() for w in words)]
        key = self.order.currentData()
        if key == "calm":
            shots.sort(key=lambda s: s.motion)
        elif key == "action":
            shots.sort(key=lambda s: -s.motion)
        elif key == "long":
            shots.sort(key=lambda s: -(s.end - s.start))
        elif key == "people":
            shots.sort(key=lambda s: -int(self.tags.get(s.id, {}).get("people") or 0))
        elif key == "marked":
            shots.sort(key=lambda s: {"★ ": 0, "": 1, "✕ ": 2}[self._mark(s.id)])
        self.grid.clear()
        names = len(self.footage.sources) > 1
        for s in shots:
            src = Path(self.footage.sources[s.source].path).stem[:12] + " " if names else ""
            tag = self.tags.get(s.id, {})
            people = f" · {tag['people']}👤" if tag.get("people") else ""
            item = QListWidgetItem(f"{self._mark(s.id)}{src}{_mmss(s.start)} · {s.end - s.start:.1f}s{people}")
            pix = QPixmap(s.thumb) if s.thumb and Path(s.thumb).is_file() else QPixmap()
            if not pix.isNull():
                item.setIcon(QIcon(pix.scaled(208, 117, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            if tag.get("desc"):
                item.setToolTip(tag["desc"])
            item.setData(Qt.UserRole, s.id)
            self.grid.addItem(item)
        if q and not shots:
            self.count.setText("No shots match." + ("" if self.tags else " Searching needs Describe my shots (in the "
                                                                           "Footage section) first."))

    def _mark_menu(self, pos):
        item = self.grid.itemAt(pos)
        if item is None:
            return
        sid = int(item.data(Qt.UserRole))
        menu = QMenu(self)
        fav = menu.addAction("★ Favourite (use it more, at the big moments)")
        ban = menu.addAction("✕ Never use this shot")
        if self._mark(sid):
            menu.addAction("Clear the mark")
        chosen = menu.exec(self.grid.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        self.project.mark_shot(sid, "favorite" if chosen is fav else "banned" if chosen is ban else None)
        self.marks_changed = True
        self._fill()

    def _selection_changed(self):
        now = [int(i.data(Qt.UserRole)) for i in self.grid.selectedItems()]
        self.picked = [i for i in self.picked if i in now] + [i for i in now if i not in self.picked]
        if self.browse:
            tag = self.tags.get(now[0], {}) if now else {}
            self.count.setText(tag.get("desc", ""))
            return
        many = len(self.picked) > 1
        self.freeze.setEnabled(not many)
        self.ok.setText(f"Use these {len(self.picked)} shots" if many else "Use this shot")
        self.count.setText(f"{len(self.picked)} shots picked: they play in the order you clicked them, one per cut "
                           "(your taps stay where they are)." if many else
                           self.tags.get(self.picked[0], {}).get("desc", "") if self.picked else "")

    def accept(self):
        if self.browse:
            return super().accept()
        if not self.picked:
            item = self.grid.currentItem()
            if item is None:
                return
            self.picked = [int(item.data(Qt.UserRole))]
        self.chosen = list(self.picked)
        super().accept()


class InstructionDialog(QDialog):
    """Right-click > Insert instruction here: say what should happen from this point."""

    def __init__(self, parent, t: float, choices: list[tuple[str, float, float]], default: int = 0, helper=None):
        super().__init__(parent)
        self.helper = helper
        self.setWindowTitle(f"Instruction at {_mmss(t)}")
        self.resize(560, 300)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("What should happen here?"))
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("e.g. \"a shot with as many characters as possible\", \"quick cuts with punches\", "
                                     "\"hold one shot\", \"crossfades\", \"flash on every snare\", "
                                     "\"freeze\", text \"THE END\"")
        v.addWidget(self.text, 1)
        self.bubble = TipBubble()
        v.addWidget(self.bubble)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self.helper and self.bubble.show_help(
            *self.helper(self.text.toPlainText(), "part")))
        self.text.textChanged.connect(lambda: self._timer.start(350))
        row = QHBoxLayout()
        row.addWidget(QLabel("For:"))
        self.scope = QComboBox()
        for label, a, b in choices:
            self.scope.addItem(label, (a, b))
        self.scope.setCurrentIndex(default)
        row.addWidget(self.scope, 1)
        v.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self.text.setFocus()


class TextCards(QDialog):
    """Titles, captions and credits."""

    def __init__(self, parent, texts: list[dict], length: float, start: float):
        super().__init__(parent)
        from ..titles import STYLE_NAMES

        self.setWindowTitle("Text cards")
        self.resize(640, 420)
        self.texts = [dict(t) for t in texts]
        self.length, self.start = length, start
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Titles, captions and credits drawn over the video (classic AMV style)."))
        self.list = QListWidget()
        v.addWidget(self.list, 1)
        form = QFormLayout()
        self.text = QLineEdit()
        self.text.setPlaceholderText("e.g. VEGETA - AWAKE")
        self.style = QComboBox()
        for key, name in STYLE_NAMES.items():
            self.style.addItem(name, key)
        self.when = QLineEdit()
        self.when.setPlaceholderText("start, end, or a time like 1:20")
        self.seconds = QLineEdit("3")
        form.addRow("Text", self.text)
        form.addRow("Style", self.style)
        form.addRow("When", self.when)
        form.addRow("Seconds on screen", self.seconds)
        v.addLayout(form)
        row = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(self._add)
        rem = QPushButton("Remove selected")
        rem.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch()
        v.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self._refresh()

    def _refresh(self):
        self.list.clear()
        for t in self.texts:
            when = {-2.0: "start", -1.0: "end"}.get(float(t.get("time", -2)), _mmss(float(t.get("time", 0))))
            self.list.addItem(f"“{t['text']}” · {t.get('style', 'title')} · {when} · {t.get('duration', 3):g}s")

    def _add(self):
        text = self.text.text().strip()
        if not text:
            return
        w = self.when.text().strip().lower()
        at = -2.0 if w in ("", "start", "beginning") else -1.0 if w in ("end", "ending") else _parse_time(w)
        if at is None:
            QMessageBox.warning(self, APP_NAME, "Write the time as start, end, or minutes:seconds like 1:20.")
            return
        try:
            secs = max(0.5, float(self.seconds.text() or 3))
        except ValueError:
            secs = 3.0
        self.texts.append({"text": text, "style": self.style.currentData(), "time": at, "duration": secs})
        self.text.clear()
        self._refresh()

    def _remove(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.texts):
            del self.texts[row]
            self._refresh()


class SceneKeys(QDialog):
    """What keys 1-9 put on a tapped cut."""

    def __init__(self, parent, keys: dict[str, str]):
        super().__init__(parent)
        from ..project import SCENE_PRESETS

        self.setWindowTitle("Scene keys")
        v = QVBoxLayout(self)
        v.addWidget(QLabel("While tapping, press a number to cut to that kind of shot:"))
        form = QFormLayout()
        self.combos = {}
        for k in "123456789":
            c = QComboBox()
            for pid, (label, _) in SCENE_PRESETS.items():
                c.addItem(label, pid)
            c.setCurrentIndex(max(0, c.findData(keys.get(k, "any"))))
            form.addRow(f"Key {k}", c)
            self.combos[k] = c
        v.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def keys(self) -> dict[str, str]:
        return {k: c.currentData() for k, c in self.combos.items()}
