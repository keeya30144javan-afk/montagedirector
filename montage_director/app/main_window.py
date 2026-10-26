"""Montage Director main window."""

from __future__ import annotations

import html
import logging
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QScrollBar, QSizePolicy,
                               QSlider,
                               QStackedLayout, QVBoxLayout, QWidget)

from .. import __version__
from ..direction import STYLES
from ..director_ai import MODELS
from ..footage import VIDEO_EXTENSIONS
from ..music import AUDIO_EXTENSIONS
from ..paths import APP_NAME, data_dir, workspace_dir
from ..project import EXPORT_PRESETS, Project, Settings
from . import store, theme
from .widgets import DropList, Segmented, StyleGrid, Timeline
from .worker import start_job

log = logging.getLogger(__name__)
ASSETS = Path(__file__).resolve().parent.parent / "assets"

SHAPES = [("auto", "Auto (from your idea)"), ("16:9", "16:9 widescreen"), ("9:16", "9:16 vertical (TikTok, Shorts)"),
          ("1:1", "1:1 square"), ("4:3", "4:3 classic")]
FITS = [("fill", "Crop to fill"), ("blur", "Blurred background"), ("fit", "Fit with black bars")]
FPS = [24, 30, 60]
PROMPT_HINT = ("Describe the edit you see in your head. For example:\n"
               "\"Slow emotional intro on the quiet scenes, then go crazy on the drop with the fight "
               "scenes and lots of flashes. Freeze frame at 1:05. Vertical for TikTok.\"")


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _parse_time(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        if ":" in text:
            m, s = text.split(":", 1)
            return int(m) * 60 + float(s)
        return float(text)
    except ValueError:
        raise ValueError(f"'{text}' isn't a time. Use minutes:seconds, like 1:05.") from None


def open_in_system(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - the user's own file
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def section_title(number: str, text: str) -> QLabel:
    label = QLabel(f"{number}   {text}")
    label.setObjectName("H2")
    return label


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


TAP_KEYS = {Qt.Key_Space: "cut", Qt.Key_F: "flash", Qt.Key_Z: "zoom", Qt.Key_X: "shake", Qt.Key_D: "drop"}
TAP_HELP = "Tapping: Space = cut · F = flash · Z = zoom · X = shake · D = drop · Backspace = undo"


IDEAS = [
    ("Hype fight edit", "Still shot for the intro, then when the bass drops go crazy with fight scenes and flashes "
                        "on every snare. Build up the tension with faster and faster cuts before each drop."),
    ("Sad / emotional", "Slow emotional edit with close-ups and faces, long shots, no flashes, slow motion on the "
                        "chorus, black and white. End on a freeze frame."),
    ("Phonk edit", "Phonk edit, cut on the kick, shake on every 808, dark scenes, vertical for TikTok."),
    ("2000s AMV", "Old 2008 YouTube AMV feel with glow and ghost trails, white flash into the drop, "
                  "zoom punches on the chorus."),
    ("Timeline (with times)", "At the start hold one shot. At 0:45 quick cuts of punches. At 1:20 slow down. "
                              "Around 2:00 when the solo starts slower cuts. The last 10 seconds fade to black."),
    ("Velocity edit", "Velocity edit on the drop, slow motion on the big hits, bright explosions in the chorus."),
]
PROMPT_HELP = """<b>The free director understands:</b><br><br>
<b>Pace:</b> fast, quick, crazy, hype, slow, calm, chill, "a little faster", "super slow", "not too fast",
"back to the flow", "cut every 2 beats / every bar", "faster and faster" (a build-up), velocity edit.<br><br>
<b>Effects, with amounts:</b> flashes, zoom punches, shake, glitch / RGB, slow motion, reverse, strobe:
"more", "a few", "less", "no". "Keep it simple", "smooth transitions", "no effects".<br><br>
<b>On the drums:</b> "cut on the kick", "cuts on the snare", "edits on the hits", "flash on every snare",
"shake on every 808", "slow motion on the big hits".<br><br>
<b>What to show:</b> fight scenes, punches, explosions, fire, power-ups, close-ups, faces, calm scenes,
dark / bright scenes, colours (blue, red, gold...), panning shots, "hold one shot".<br><br>
<b>Where:</b> the intro, verse, chorus, drop, bridge, outro; "the second drop", "the last chorus";
"at 1:20", "1 minute 30", "from 0:30 to 0:45", "until 0:11", "the last 20 seconds", "halfway through";
musical moments it hears: "when the bass drops", "when the drums get faster", "when the solo starts",
"the hard hits", "when the vocals come in", "before the drop", "when the last note rings".<br><br>
<b>Look and shape:</b> black and white, VHS, Y2K, film / trailer, sad, dreamy, glow, ghost trails, letterbox,
vertical / TikTok, square; "no repeats".<br><br>
The free director picks shots by how they look and move (action, calm, dark, bright, colour), not by who
is in them. To get a particular shot (a character, a group shot), select that part of the timeline and
press <b>Pick shot…</b>, or use the AI Director.<br><br>
Anything it doesn't understand is listed under the preview as <i>Didn't catch</i>. The AI Director
understands any wording."""


class ShotPicker(QDialog):
    """Choose shots from the footage, by thumbnail: one to hold, or several for the cuts in that part."""

    def __init__(self, parent, footage, seconds: float):
        super().__init__(parent)
        self.setWindowTitle("Pick a shot")
        self.resize(980, 640)
        self.chosen = None
        self.footage = footage
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel(f"Click one shot to hold it for this {seconds:.1f} s part, or Ctrl+click several "
                             "to use them in that order on the cuts here."))
        top.addStretch()
        top.addWidget(QLabel("Show:"))
        self.order = QComboBox()
        for key, label in (("time", "In footage order"), ("calm", "Calmest first"), ("action", "Most action first"),
                           ("long", "Longest first")):
            self.order.addItem(label, key)
        self.order.currentIndexChanged.connect(self._fill)
        top.addWidget(self.order)
        v.addLayout(top)
        self.grid = QListWidget()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setIconSize(QSize(208, 117))
        self.grid.setGridSize(QSize(224, 150))
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QListWidget.ExtendedSelection)
        self.grid.itemDoubleClicked.connect(lambda _: self.accept())
        self.picked: list[int] = []  # in the order they were clicked
        self.grid.itemSelectionChanged.connect(self._selection_changed)
        v.addWidget(self.grid, 1)
        self.count = QLabel("")
        self.count.setObjectName("Muted")
        v.addWidget(self.count)
        self.freeze = QCheckBox("Freeze on this frame (a still picture instead of the moving clip)")
        self.freeze.setChecked(seconds > 4.0)
        v.addWidget(self.freeze)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = buttons.button(QDialogButtonBox.Ok)
        self.ok.setText("Use this shot")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self._fill()

    def _fill(self):
        shots = [s for s in self.footage.shots if s.usable]
        key = self.order.currentData()
        if key == "calm":
            shots.sort(key=lambda s: s.motion)
        elif key == "action":
            shots.sort(key=lambda s: -s.motion)
        elif key == "long":
            shots.sort(key=lambda s: -(s.end - s.start))
        self.grid.clear()
        names = len(self.footage.sources) > 1
        for s in shots:
            src = Path(self.footage.sources[s.source].path).stem[:14] + " " if names else ""
            item = QListWidgetItem(f"{src}{_mmss(s.start)} · {s.end - s.start:.1f}s")
            pix = QPixmap(s.thumb) if s.thumb and Path(s.thumb).is_file() else QPixmap()
            if not pix.isNull():
                item.setIcon(QIcon(pix.scaled(208, 117, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            item.setData(Qt.UserRole, s.id)
            self.grid.addItem(item)

    def _selection_changed(self):
        now = [int(i.data(Qt.UserRole)) for i in self.grid.selectedItems()]
        self.picked = [i for i in self.picked if i in now] + [i for i in now if i not in self.picked]
        many = len(self.picked) > 1
        self.freeze.setEnabled(not many)
        self.ok.setText(f"Use these {len(self.picked)} shots" if many else "Use this shot")
        self.count.setText(f"{len(self.picked)} shots picked: they play in the order you clicked them, one per cut "
                           "(your taps stay where they are)." if many else "")

    def accept(self):
        if not self.picked:
            item = self.grid.currentItem()
            if item is None:
                return
            self.picked = [int(item.data(Qt.UserRole))]
        self.chosen = list(self.picked)
        super().accept()


class TapFilter(QObject):
    """While tapping, every key press goes to Tap to Direct (so Space doesn't press a button)."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and not event.isAutoRepeat():
            if event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
                return True  # no shortcuts (like Ctrl+Z) while tapping
            key = event.key()
            if key in TAP_KEYS or key in (Qt.Key_Backspace, Qt.Key_Escape):
                self.window.on_tap_key(key)
                return True
        return False


class MainWindow(QMainWindow):
    REFINE_HINT = ("Tell it what to change, e.g. \"more flashes in the drop\". Or drag across the timeline "
                   "to change just that part.")

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1360, 860)
        self.setMinimumSize(QSize(1060, 680))
        self.saved = store.load()
        self.project = Project()
        self.thread = None
        self.job = None
        self.busy_kind = ""
        self.player = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_main(), 1)
        self._build_menus()
        self._restore()
        self._update_director_info()
        self._set_busy(False)
        QTimer.singleShot(3000, self.check_for_updates)

    # ----------------------------------------------------------------- layout
    def _build_menus(self):
        def action(menu, text, slot, keys=()):
            a = QAction(text, self)
            a.setShortcuts([QKeySequence(k) for k in keys])
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        file = self.menuBar().addMenu("&File")
        action(file, "New project", self.new_project, ["Ctrl+N"])
        action(file, "Open project...", self.open_project, [QKeySequence.Open])
        file.addSeparator()
        action(file, "Save project", self.save_project, [QKeySequence.Save])
        action(file, "Save project as...", self.save_project_as, ["Ctrl+Shift+S"])
        file.addSeparator()
        action(file, "Export video...", self.export, ["Ctrl+E"])
        edit = self.menuBar().addMenu("&Edit")
        self.undo_action = action(edit, "Undo change", self.undo, [QKeySequence.Undo])
        self.redo_action = action(edit, "Redo change", self.redo, ["Ctrl+Y", "Ctrl+Shift+Z"])
        edit.addSeparator()
        action(edit, "Compare before / after", lambda: self.compare_btn.isEnabled() and self.compare_btn.toggle(),
               ["C"])

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(392)
        outer = QVBoxLayout(side)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("H1")
        head.addWidget(title)
        head.addStretch()
        gear = QPushButton("Settings")
        gear.clicked.connect(self.open_settings)
        head.addWidget(gear)
        v.addLayout(head)
        v.addSpacing(6)

        v.addWidget(section_title("1", "FOOTAGE"))
        self.footage_list = DropList(VIDEO_EXTENSIONS, placeholder="Drop episodes, gameplay or clips here")
        v.addWidget(self.footage_list)
        row = QHBoxLayout()
        for text, slot in (("Add videos", self.add_videos), ("Add folder", self.add_folder),
                           ("Remove", self.footage_list.remove_selected)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        v.addLayout(row)
        self.footage_info = QLabel("")
        self.footage_info.setObjectName("Muted")
        v.addWidget(self.footage_info)
        self.footage_list.changed.connect(self._inputs_changed)

        v.addSpacing(6)
        v.addWidget(section_title("2", "SONG"))
        row = QHBoxLayout()
        self.song_list = DropList(AUDIO_EXTENSIONS | VIDEO_EXTENSIONS, single=True, placeholder="Drop a song here")
        row.addWidget(self.song_list, 1)
        b = QPushButton("Choose")
        b.clicked.connect(self.choose_song)
        row.addWidget(b)
        v.addLayout(row)
        self.song_list.changed.connect(self._inputs_changed)
        rng = QHBoxLayout()
        rng.addWidget(QLabel("Use part of it:"))
        self.start_edit = QLineEdit()
        self.start_edit.setPlaceholderText("0:00")
        self.end_edit = QLineEdit()
        self.end_edit.setPlaceholderText("end")
        for e in (self.start_edit, self.end_edit):
            e.setFixedWidth(64)
            e.setToolTip("minutes:seconds")
        rng.addWidget(self.start_edit)
        rng.addWidget(QLabel("to"))
        rng.addWidget(self.end_edit)
        rng.addStretch()
        v.addLayout(rng)
        self.song_info = QLabel("")
        self.song_info.setObjectName("Muted")
        v.addWidget(self.song_info)

        v.addSpacing(6)
        v.addWidget(section_title("3", "STYLE"))
        self.styles = StyleGrid(self.saved.get("style", "hype"))
        self.styles.changed.connect(lambda sid: self.style_info.setText(STYLES[sid].description))
        v.addWidget(self.styles)
        self.style_info = QLabel(STYLES[self.styles.selected()].description)
        self.style_info.setWordWrap(True)
        self.style_info.setObjectName("Muted")
        v.addWidget(self.style_info)

        v.addSpacing(6)
        v.addWidget(section_title("4", "YOUR IDEA"))
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(PROMPT_HINT)
        self.prompt.setFixedHeight(120)
        v.addWidget(self.prompt)
        row = QHBoxLayout()
        ideas = QPushButton("Ideas ▾")
        ideas.setToolTip("Example ideas to start from (added to what you've written)")
        menu = QMenu(ideas)
        for label, text in IDEAS:
            act = menu.addAction(label)
            act.triggered.connect(lambda _=False, t=text: self.add_idea(t))
        ideas.setMenu(menu)
        row.addWidget(ideas)
        help_btn = QPushButton("What can I say?")
        help_btn.clicked.connect(self.show_prompt_help)
        row.addWidget(help_btn)
        row.addStretch()
        v.addLayout(row)

        v.addSpacing(6)
        v.addWidget(section_title("5", "DIRECTOR"))
        self.director = Segmented(["Free (on this PC)", "AI Director"])
        self.director.changed.connect(lambda _: self._update_director_info())
        v.addWidget(self.director)
        self.director_info = QLabel("")
        self.director_info.setWordWrap(True)
        self.director_info.setObjectName("Muted")
        v.addWidget(self.director_info)

        v.addSpacing(6)
        v.addWidget(section_title("6", "OUTPUT"))
        form = QFormLayout()
        form.setSpacing(6)
        self.shape = QComboBox()
        for key, label in SHAPES:
            self.shape.addItem(label, key)
        self.fit = QComboBox()
        for key, label in FITS:
            self.fit.addItem(label, key)
        self.fps = QComboBox()
        for f in FPS:
            self.fps.addItem(f"{f} fps", f)
        form.addRow("Shape", self.shape)
        form.addRow("Framing", self.fit)
        form.addRow("Frame rate", self.fps)
        v.addLayout(form)

        v.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(18, 10, 18, 16)
        self.make_btn = QPushButton("Make Edit")
        self.make_btn.setObjectName("Primary")
        self.make_btn.clicked.connect(self.make_edit)
        bl.addWidget(self.make_btn)
        outer.addWidget(bottom)
        return side

    def _build_main(self) -> QWidget:
        main = QWidget()
        v = QVBoxLayout(main)
        v.setContentsMargins(18, 16, 18, 14)
        v.setSpacing(10)

        # Video
        self.video_frame = QFrame()
        self.video_frame.setStyleSheet(f"background: black; border-radius: 8px;")
        self.video_stack = QStackedLayout(self.video_frame)
        self.placeholder = QLabel("Add footage and a song, describe your idea,\nthen press Make Edit.")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(f"color: {theme.MUTED}; font-size: 15px;")
        self.video_stack.addWidget(self.placeholder)
        self._init_player()
        v.addWidget(self.video_frame, 1)

        controls = QHBoxLayout()
        self.play_btn = QPushButton("Play")
        self.play_btn.setFixedWidth(80)
        self.play_btn.clicked.connect(self.toggle_play)
        controls.addWidget(self.play_btn)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.sliderMoved.connect(lambda ms: self.player and self.player.setPosition(ms))
        controls.addWidget(self.slider, 1)
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("Muted")
        controls.addWidget(self.time_label)
        self.tap_btn = QPushButton("Tap to Direct")
        self.tap_btn.setToolTip("Play the edit and tap along: Space = cut, F = flash, Z = zoom, X = shake, "
                                "D = drop, Backspace = undo. Your taps snap to the nearest drum hit.")
        self.tap_btn.clicked.connect(self.toggle_tapping)
        controls.addWidget(self.tap_btn)
        self.compare_btn = QPushButton("Before / After")
        self.compare_btn.setCheckable(True)
        self.compare_btn.setToolTip("Flip between this preview and the one before your last change, "
                                    "at the same moment in the song (shortcut: C)")
        self.compare_btn.toggled.connect(self.toggle_compare)
        controls.addWidget(self.compare_btn)
        self.open_btn = QPushButton("Open in player")
        self.open_btn.clicked.connect(self.open_preview)
        controls.addWidget(self.open_btn)
        v.addLayout(controls)

        self.timeline = Timeline()
        self.timeline.seek.connect(self.seek_seconds)
        self.timeline.drops_changed.connect(self.drops_changed)
        self.timeline.region_selected.connect(self.region_selected)
        self.timeline.region_removed.connect(self.region_removed)
        self.timeline.taps_cleared.connect(self.clear_taps)
        self.timeline.taps_removed.connect(self.remove_taps)
        self.tapping = False
        self.tap_filter = TapFilter(self)
        v.addWidget(self.timeline)
        zoom_row = QHBoxLayout()
        self.timeline_bar = QScrollBar(Qt.Horizontal)
        self.timeline_bar.setRange(0, 0)
        self.timeline_bar.valueChanged.connect(self._timeline_scrolled)
        zoom_row.addWidget(self.timeline_bar, 1)
        for text, tip, slot in (("−", "Zoom out", lambda: self.timeline.zoom(1.6)),
                                ("+", "Zoom in (or use the mouse wheel on the timeline)", lambda: self.timeline.zoom(0.6)),
                                ("Fit", "Show the whole edit", self.timeline.fit)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedWidth(44 if text != "Fit" else 52)
            b.clicked.connect(slot)
            zoom_row.addWidget(b)
        v.addLayout(zoom_row)
        self.timeline.view_changed.connect(self._timeline_view)

        self.summary = QLabel("")
        self.summary.setObjectName("Summary")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.summary.hide()
        v.addWidget(self.summary)

        refine = QHBoxLayout()
        self.region_chip = QPushButton("")
        self.region_chip.setToolTip("Refining only this part of the song. Click to refine the whole edit instead.")
        self.region_chip.setStyleSheet(f"background: #241f3d; border: 1px solid {theme.ACCENT}; color: {theme.TEXT};")
        self.region_chip.clicked.connect(self.timeline.clear_selection)
        self.region_chip.hide()
        refine.addWidget(self.region_chip)
        self.pick_btn = QPushButton("Pick shot…")
        self.pick_btn.setToolTip("Choose exactly which shot plays in the selected part (for example a group shot "
                                 "for the final note)")
        self.pick_btn.clicked.connect(self.pick_shot_for_region)
        self.pick_btn.hide()
        refine.addWidget(self.pick_btn)
        self.refine_edit = QLineEdit()
        self.refine_edit.setPlaceholderText(self.REFINE_HINT)
        self.refine_edit.returnPressed.connect(self.refine)
        refine.addWidget(self.refine_edit, 1)
        self.refine_btn = QPushButton("Refine")
        self.refine_btn.clicked.connect(self.refine)
        refine.addWidget(self.refine_btn)
        self.variation_btn = QPushButton("New variation")
        self.variation_btn.setToolTip("Same idea, different shot choices")
        self.variation_btn.clicked.connect(self.variation)
        refine.addWidget(self.variation_btn)
        self.undo_btn = QPushButton("↶")
        self.undo_btn.setToolTip("Undo the last change (Ctrl+Z)")
        self.undo_btn.setFixedWidth(40)
        self.undo_btn.clicked.connect(self.undo)
        refine.addWidget(self.undo_btn)
        self.redo_btn = QPushButton("↷")
        self.redo_btn.setToolTip("Redo (Ctrl+Y)")
        self.redo_btn.setFixedWidth(40)
        self.redo_btn.clicked.connect(self.redo)
        refine.addWidget(self.redo_btn)
        v.addLayout(refine)

        bar = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(220)
        bar.addWidget(self.progress)
        self.status = QLabel("Ready.")
        self.status.setObjectName("Muted")
        bar.addWidget(self.status, 1)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop)
        bar.addWidget(self.stop_btn)
        self.quality = QComboBox()
        for key, (label, *_rest) in EXPORT_PRESETS.items():
            self.quality.addItem(label, key)
        self.quality.setToolTip("YouTube and TikTok presets set the size and frame rate for you; Discord keeps "
                                "the file under 25 MB")
        bar.addWidget(self.quality)
        self.export_btn = QPushButton("Export video")
        self.export_btn.setObjectName("Hot")
        self.export_btn.clicked.connect(self.export)
        bar.addWidget(self.export_btn)
        folder = QPushButton("Exports folder")
        folder.clicked.connect(self.open_exports)
        bar.addWidget(folder)
        v.addLayout(bar)
        return main

    def _init_player(self):
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget
        except Exception as exc:  # multimedia backend missing: fall back to the system player
            log.warning("Built-in player unavailable: %s", exc)
            return
        self.video_widget = QVideoWidget()
        self.video_stack.addWidget(self.video_widget)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget)
        self.player.durationChanged.connect(lambda d: self.slider.setRange(0, d))
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(
            lambda s: self.play_btn.setText("Pause" if s == QMediaPlayer.PlayingState else "Play"))
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self._seek_after_load = None

    # ------------------------------------------------------------- persistence
    def _restore(self):
        s = self.saved
        self.footage_list.add_paths([p for p in s.get("footage", []) if Path(p).exists()])
        if s.get("song") and Path(s["song"]).exists():
            self.song_list.add_paths([s["song"]])
        self.prompt.setPlainText(s.get("prompt", ""))
        self.director.set_index(1 if s.get("use_ai") else 0)
        for combo, key in ((self.shape, "aspect"), (self.fit, "fit"), (self.fps, "fps")):
            i = combo.findData(s.get(key))
            if i >= 0:
                combo.setCurrentIndex(i)
        if s.get("fps") is None:
            self.fps.setCurrentIndex(1)
        self.model = s.get("model", next(iter(MODELS)))
        i = self.quality.findData(s.get("export_preset"))
        if i >= 0:
            self.quality.setCurrentIndex(i)

    def _persist(self):
        store.save({**store.load(),
                    "footage": self.footage_list.paths(), "song": (self.song_list.paths() or [""])[0],
                    "style": self.styles.selected(), "prompt": self.prompt.toPlainText(),
                    "use_ai": self.director.index() == 1, "aspect": self.shape.currentData(),
                    "fit": self.fit.currentData(), "fps": self.fps.currentData(), "model": self.model,
                    "export_preset": self.quality.currentData()})

    def closeEvent(self, e):
        if self.job is not None:
            self.stop()
        self._persist()
        super().closeEvent(e)

    # ----------------------------------------------------------------- inputs
    def add_videos(self):
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "Add footage", str(Path.home()), f"Videos ({exts})")
        self.footage_list.add_paths(files)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add a folder of footage", str(Path.home()))
        if folder:
            self.footage_list.add_paths([folder])

    def choose_song(self):
        exts = " ".join(f"*{e}" for e in sorted(AUDIO_EXTENSIONS))
        file, _ = QFileDialog.getOpenFileName(self, "Choose a song", str(Path.home()), f"Audio ({exts})")
        if file:
            self.song_list.add_paths([file])

    def _inputs_changed(self):
        self.footage_info.setText("")
        self.song_info.setText("")

    def _update_director_info(self):
        if self.director.index() == 1:
            label = MODELS.get(getattr(self, "model", ""), next(iter(MODELS.values())))[0]
            key = "" if store.get_api_key() else " Add your API key in Settings first."
            self.director_info.setText(
                f"Claude watches your footage and builds the edit from your idea. Uses your API key "
                f"({label}); usually a few cents to a few dimes per edit.{key}")
        else:
            self.director_info.setText(
                "Runs on this PC for free. Understands words like fast, slow, flashes, shake, glitch, "
                "vertical, black and white, and times like \"freeze at 1:05\".")

    def open_settings(self):
        dlg = SettingsDialog(self, getattr(self, "model", next(iter(MODELS))))
        if dlg.exec():
            self.model = dlg.model.currentData()
            self._persist()
        self._update_director_info()

    def _collect(self) -> Settings:
        start = _parse_time(self.start_edit.text()) or 0.0
        end = _parse_time(self.end_edit.text())
        return Settings(
            style=self.styles.selected(), prompt=self.prompt.toPlainText().strip(),
            use_ai=self.director.index() == 1, api_key=store.get_api_key(), model=self.model,
            aspect=self.shape.currentData(), fit=self.fit.currentData(), fps=self.fps.currentData(),
            song_start=start, song_end=end, seed=None,
        )

    # ------------------------------------------------------------------ jobs
    def _run(self, kind: str, fn, on_done):
        if self.job is not None:
            return
        self.busy_kind = kind
        self._set_busy(True)
        self.progress.setValue(0)
        # Slots must be bound methods of this window so Qt delivers them on the UI thread.
        self._pending_done = on_done
        self.thread, self.job = start_job(self, fn, self._on_progress, self._on_done,
                                          self._on_failed, self._on_cancelled)

    def _set_busy(self, busy: bool):
        has_plan = self.project.plan is not None and not busy
        if hasattr(self, "tap_btn"):
            self.tap_btn.setEnabled(has_plan)
        self.make_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        for w in (self.refine_btn, self.variation_btn, self.export_btn, self.refine_edit):
            w.setEnabled(has_plan)
        if hasattr(self, "undo_btn"):
            can_undo, can_redo = has_plan and bool(self.project.history), has_plan and bool(self.project.future)
            self.undo_btn.setEnabled(can_undo)
            self.undo_action.setEnabled(can_undo)
            self.redo_btn.setEnabled(can_redo)
            self.redo_action.setEnabled(can_redo)
            prev = self.project.previous_preview
            self.compare_btn.setEnabled(not busy and bool(prev) and Path(prev).exists())
        self.make_btn.setText({"make": "Making your edit...", "refine": "Refining...",
                               "export": "Exporting..."}.get(self.busy_kind, "Make Edit") if busy else "Make Edit")

    def _on_progress(self, frac: float, msg: str):
        self.progress.setValue(int(frac * 1000))
        self.status.setText(msg)

    def _on_done(self, result):
        on_done, self._pending_done = self._pending_done, None
        self.job = None
        self.busy_kind = ""
        self.progress.setValue(1000)
        on_done(result)
        self._set_busy(False)

    def _on_failed(self, message: str, details: str):
        self.job = None
        self.busy_kind = ""
        self._set_busy(False)
        self.progress.setValue(0)
        self.status.setText("Stopped: " + message)
        if details:
            log.error(details)
        box = QMessageBox(QMessageBox.Warning, APP_NAME, message, parent=self)
        if details:
            box.setDetailedText(details)
        box.exec()

    def _on_cancelled(self):
        self.job = None
        self.busy_kind = ""
        self._set_busy(False)
        self.progress.setValue(0)
        self.status.setText("Stopped.")

    def stop(self):
        if self.job is not None:
            self.job.cancel.set()
            self.project.stop()
            self.status.setText("Stopping...")

    # ---------------------------------------------------------------- actions
    def make_edit(self):
        try:
            settings = self._collect()
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        footage, song = self.footage_list.paths(), (self.song_list.paths() or [""])[0]
        if not footage:
            QMessageBox.information(self, APP_NAME, "Add some footage first: episodes, gameplay or clips.")
            return
        if not song:
            QMessageBox.information(self, APP_NAME, "Add a song first.")
            return
        if settings.use_ai and not settings.api_key:
            QMessageBox.information(self, APP_NAME, "The AI Director needs your Anthropic API key. "
                                    "Add it in Settings, or switch to the Free director.")
            self.open_settings()
            return
        self._persist()
        proj = self.project
        if (footage, song) != (proj.footage_paths, proj.song_path):
            proj.history.clear()  # other footage or song: old edits don't apply
            proj.future.clear()
        else:
            proj.checkpoint()
        proj.footage_paths, proj.song_path, proj.settings = footage, song, settings
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())

        def work(cancel, progress):
            proj.analyse(cancel, lambda f, m: progress(0.35 * f, m))
            progress(0.36, "Directing the edit with Claude..." if settings.use_ai else "Directing the edit...")
            proj.direct(cancel, lambda f, m: progress(0.36 + 0.06 * f, m))
            progress(0.42, "Planning cuts on the beat...")
            proj.make_plan()
            proj.render_preview(cancel, lambda f, m: progress(0.42 + 0.58 * f, m))
            return True

        self._run("make", work, lambda _: self._show_result())

    def region_selected(self, a: float, b: float):
        if b <= a:
            self.region = None
            self.region_chip.hide()
            self.pick_btn.hide()
            self.refine_edit.setPlaceholderText(self.REFINE_HINT)
            return
        self.region = (a, b)
        self.region_chip.setText(f"{_mmss(a)}–{_mmss(b)}  ✕")
        self.region_chip.show()
        self.pick_btn.setVisible(self.project.plan is not None)
        self.refine_edit.setPlaceholderText(f"What should happen from {_mmss(a)} to {_mmss(b)}? e.g. "
                                            "\"quicker cuts with punches\", \"hold one shot\", \"cut on the hits\"")
        self.refine_edit.setFocus()

    def pick_shot_for_region(self):
        region = getattr(self, "region", None)
        proj = self.project
        if not region or proj.footage is None or self.job is not None:
            return
        a, b = region
        dlg = ShotPicker(self, proj.footage, b - a)
        if dlg.exec() != QDialog.Accepted or dlg.chosen is None:
            return
        proj.checkpoint()
        proj.pick_region_shot(a, b, dlg.chosen, dlg.freeze.isChecked() and len(dlg.chosen) == 1)
        self.timeline.clear_selection()
        what = "your shot" if len(dlg.chosen) == 1 else f"your {len(dlg.chosen)} shots"
        self._replan(f"Putting {what} in {_mmss(a)}–{_mmss(b)}...")

    # ------------------------------------------------------------ tap to direct
    def toggle_tapping(self):
        if self.tapping:
            self.stop_tapping(apply=True)
            return
        if self.project.plan is None or self.job is not None:
            return
        if self.player is None:
            QMessageBox.information(self, APP_NAME, "Tap to Direct needs the built-in player, which isn't "
                                    "available on this computer.")
            return
        self.tapping = True
        self._taps_before = list(self.project.taps)
        QApplication.instance().installEventFilter(self.tap_filter)
        self.tap_btn.setText("Done tapping")
        self.tap_btn.setStyleSheet(f"background: {theme.HOT}; color: white; border: none; font-weight: 700;")
        self.status.setText(TAP_HELP)
        self.setFocus()
        self.player.play()

    def stop_tapping(self, apply: bool):
        self.tapping = False
        QApplication.instance().removeEventFilter(self.tap_filter)
        self.tap_btn.setText("Tap to Direct")
        self.tap_btn.setStyleSheet("")
        if self.player:
            self.player.pause()
        if not apply:
            self.project.taps = self._taps_before
            self.timeline.set_taps(self.project.taps)
            self.status.setText("Tapping cancelled.")
            return
        if self.project.taps == self._taps_before:
            self.status.setText("No new taps.")
            return
        self.project.checkpoint()
        self.project.history[-1]["taps"] = list(self._taps_before)  # undo goes back to before tapping
        self.project.apply_taps()
        self._replan(f"Rebuilding the edit around your {len(self.project.taps)} taps...")

    def on_tap_key(self, key):
        if key == Qt.Key_Escape:
            self.stop_tapping(apply=False)
            return
        taps = self.project.taps
        if key == Qt.Key_Backspace:
            if taps:
                taps.pop()
        else:
            plan = self.project.plan
            # what you hear now, minus a typical reaction delay
            t = plan.song_start + max(0.0, self.player.position() / 1000.0 - 0.06)
            taps.append((round(t, 3), TAP_KEYS[key]))  # kept in pressing order, so Backspace undoes the last press
        self.timeline.set_taps(taps)
        counts = {k: sum(1 for _, kind in taps if kind == k) for k in ("cut", "flash", "zoom", "shake", "drop")}
        summary = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in counts.items() if n)
        self.status.setText(f"{TAP_HELP}   [{summary or 'no taps yet'}]")

    def _timeline_view(self, a: float, b: float):
        tl = self.timeline
        full = max(tl.full_b - tl.full_a, 1e-6)
        self.timeline_bar.blockSignals(True)
        self.timeline_bar.setRange(0, int(round((full - (b - a)) * 100)))
        self.timeline_bar.setPageStep(max(1, int((b - a) * 100)))
        self.timeline_bar.setValue(int(round((a - tl.full_a) * 100)))
        self.timeline_bar.blockSignals(False)

    def _timeline_scrolled(self, value: int):
        self.timeline.scroll_to(self.timeline.full_a + value / 100.0)

    def remove_taps(self, times: list):
        if self.job is not None or self.tapping or not times:
            return
        self.project.checkpoint()
        n = self.project.remove_taps(times)
        self.timeline.set_taps(self.project.taps)
        self._replan(f"Taking out {n} tap{'s' if n != 1 else ''}...")

    def clear_taps(self):
        if self.job is not None or not self.project.taps:
            return
        self.project.checkpoint()
        self.project.clear_taps()
        self.timeline.set_taps([])
        self._replan("Removing your taps...")

    def region_removed(self, t: float):
        if self.job is not None:
            return
        self.project.checkpoint()
        if not self.project.remove_region(t):
            self.project.history.pop()
            return
        self._replan("Undoing that section edit...")

    def _replan(self, message: str):
        proj = self.project

        def work(cancel, progress):
            progress(0.05, message)
            proj.make_plan()
            proj.render_preview(cancel, lambda f, m: progress(0.1 + 0.9 * f, m))
            return True

        self._run("refine", work, lambda _: self._show_result())

    def refine(self):
        text = self.refine_edit.text().strip()
        if not text or self.project.plan is None:
            return
        proj = self.project
        proj.checkpoint()
        region = getattr(self, "region", None)
        if region:
            a, b = region

            def work_region(cancel, progress):
                progress(0.05, f"Refining {_mmss(a)}–{_mmss(b)}...")
                notes = proj.refine_region(text, a, b)
                proj.make_plan()
                proj.render_preview(cancel, lambda f, m: progress(0.15 + 0.85 * f, m))
                return notes

            def done_region(notes):
                self.refine_edit.clear()
                self.timeline.clear_selection()
                self._show_result()

            self._run("refine", work_region, done_region)
            return

        def work(cancel, progress):
            progress(0.05, "Refining with Claude..." if proj.settings.use_ai else "Applying your changes...")
            proj.refine(text)
            proj.make_plan()
            proj.render_preview(cancel, lambda f, m: progress(0.15 + 0.85 * f, m))
            return True

        self._run("refine", work, lambda _: (self.refine_edit.clear(), self._show_result()))

    def variation(self):
        if self.project.plan is None:
            return
        proj = self.project
        proj.checkpoint()

        def work(cancel, progress):
            proj.reroll()
            proj.render_preview(cancel, progress)
            return True

        self._run("refine", work, lambda _: self._show_result())

    def drops_changed(self, drops: list):
        """The user marked or removed a drop on the timeline: re-plan around it."""
        proj = self.project
        if proj.plan is None or self.job is not None:
            return
        manual = self.timeline.drops  # None when they asked for the detected drops again
        proj.checkpoint()
        proj.manual_drops = manual
        proj.direction.drops = manual if manual is not None else None

        def work(cancel, progress):
            progress(0.05, "Re-planning around the drop...")
            proj.make_plan()
            proj.render_preview(cancel, lambda f, m: progress(0.1 + 0.9 * f, m))
            return True

        self._run("refine", work, lambda _: self._show_result())

    def export(self):
        if self.project.plan is None:
            return
        default = self.project.default_export_path()
        path, _ = QFileDialog.getSaveFileName(self, "Export video", str(default), "MP4 video (*.mp4)")
        if not path:
            return
        if not path.lower().endswith(".mp4"):
            path += ".mp4"
        preset = self.quality.currentData()
        self._persist()

        def work(cancel, progress):
            return self.project.export(path, preset, cancel, progress)

        def done(out):
            self.status.setText(f"Exported {Path(out).name}")
            box = QMessageBox(QMessageBox.Information, APP_NAME, f"Your edit is saved:\n{out}", parent=self)
            open_btn = box.addButton("Show in folder", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Ok)
            box.exec()
            if box.clickedButton() is open_btn:
                open_in_system(Path(out).parent)

        self._run("export", work, done)

    def _show_result(self):
        proj = self.project
        plan = proj.plan
        self.footage_info.setText(proj.footage.summary())
        song = proj.song
        self.song_info.setText(f"{song.tempo:.0f} BPM · {len(song.sections)} sections · "
                               f"{len(song.drops)} drop(s) · {_mmss(song.duration)}")
        self.timeline.set_song(song)
        self.timeline.set_plan(plan)
        self.timeline.set_drops(proj.direction.drops if proj.direction.drops is not None else None)
        self.timeline.set_regions([(r.start, r.end, r.note) for r in proj.direction.regions])
        self.timeline.set_taps(proj.taps)
        d = proj.direction
        lines = []
        from ..listening import describe

        heard = describe(song.events)
        feel = song.feel.get("words") if song.feel else None
        if feel:
            lines.append(f"<b>Feel:</b> <span style='color:{theme.MUTED}'>" + " · ".join(feel) + "</span>")
        if heard:
            lines.append(f"<b>Heard:</b> <span style='color:{theme.MUTED}'>" + " · ".join(heard) + "</span>")
        if plan.timeline:
            lines.append("<b>Your timeline:</b> <span style='color:%s'>%s</span>" % (theme.TEXT, " · ".join(plan.timeline)))
        if d.summary:
            lines.append(f"<b>Director:</b> {d.summary}")
        # Timed instructions (⏱) are already listed as placed above; show the rest.
        shown = [n for n in d.notes if not (plan.timeline and n.startswith("⏱"))]
        shown = [n.lstrip("⏱ ") for n in shown]
        if shown:
            lines.append("<span style='color:%s'>%s</span>" % (theme.MUTED, " · ".join(shown)))
        if d.unclear and not proj.settings.use_ai:
            quoted = " · ".join(f"“{html.escape(u)}”" for u in d.unclear[:4])
            lines.append(f"<span style='color:{theme.WARN}'>Didn't catch: {quoted}</span> "
                         f"<span style='color:{theme.MUTED}'>(try pace, effects, the kind of shots, or a time like "
                         f"\"at 1:20\"; the AI Director understands anything)</span>")
        for w in plan.warnings:
            lines.append(f"<span style='color:{theme.WARN}'>⚠ {w}</span>")
        cost = f" · AI cost so far: ${proj.ai.usage.cost:.2f}" if proj.ai else ""
        lines.append(f"<span style='color:{theme.MUTED}'>{len(plan.cuts)} cuts · {plan.width}×{plan.height} · "
                     f"{_mmss(plan.duration)}{cost}</span>")
        self.summary.setText("<br>".join(lines))
        self.summary.show()
        self.status.setText("Preview ready." + ("  Press Before / After (C) to compare with the last version."
                                                if proj.previous_preview else ""))
        self.compare_btn.blockSignals(True)
        self.compare_btn.setChecked(False)
        self.compare_btn.setText("Before / After")
        self.compare_btn.blockSignals(False)
        self.load_preview(proj.preview_path)
        name = proj.path.stem if proj.path else ""
        self.setWindowTitle(f"{name} · {APP_NAME}" if name else APP_NAME)

    def add_idea(self, text: str):
        cur = self.prompt.toPlainText().strip()
        self.prompt.setPlainText((cur + "\n" if cur else "") + text)

    def show_prompt_help(self):
        box = QMessageBox(QMessageBox.Information, "What can I say?", PROMPT_HELP, parent=self)
        box.setTextFormat(Qt.RichText)
        box.exec()

    # ----------------------------------------------------------- undo / redo
    def undo(self):
        if self.job is not None or self.tapping or not self.project.undo():
            return
        self.timeline.clear_selection()
        self._replan("Undoing your last change...")

    def redo(self):
        if self.job is not None or self.tapping or not self.project.redo():
            return
        self.timeline.clear_selection()
        self._replan("Redoing...")

    # --------------------------------------------------------------- compare
    def toggle_compare(self, before: bool):
        proj = self.project
        path = proj.previous_preview if before else proj.preview_path
        if not path or not Path(path).exists():
            self.compare_btn.setChecked(False)
            return
        self.compare_btn.setText("Showing: Before" if before else "Before / After")
        self.status.setText("Showing the version BEFORE your last change. Press C to flip back." if before
                            else "Showing your latest version.")
        if self.player is None:
            open_in_system(Path(path))
            return
        from PySide6.QtMultimedia import QMediaPlayer

        self._seek_after_load = (self.player.position(), self.player.playbackState() == QMediaPlayer.PlayingState)
        self.player.setSource(QUrl.fromLocalFile(str(path)))

    def _on_media_status(self, status):
        from PySide6.QtMultimedia import QMediaPlayer

        if self._seek_after_load and status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            pos, playing = self._seek_after_load
            self._seek_after_load = None
            self.player.setPosition(pos)
            if playing:
                self.player.play()

    # -------------------------------------------------------------- projects
    def _projects_dir(self) -> Path:
        folder = workspace_dir() / "Projects"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def new_project(self):
        if self.job is not None:
            return
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())
        self.project = Project()
        self.footage_list.clear()
        self.song_list.clear()
        self.prompt.clear()
        self.start_edit.clear()
        self.end_edit.clear()
        self.summary.hide()
        self.timeline.set_plan(None)
        self.footage_info.setText("")
        self.song_info.setText("")
        self.video_stack.setCurrentWidget(self.placeholder)
        self.setWindowTitle(APP_NAME)
        self.status.setText("New project.")
        self._set_busy(False)

    def save_project(self):
        if self.project.path:
            self._write_project(self.project.path)
        else:
            self.save_project_as()

    def save_project_as(self):
        proj = self.project
        default = proj.path or self._projects_dir() / ((Path(proj.song_path).stem if proj.song_path else "My edit")
                                                        + ".mdproj")
        path, _ = QFileDialog.getSaveFileName(self, "Save project", str(default), "Montage Director project (*.mdproj)")
        if path:
            self._write_project(Path(path))

    def _write_project(self, path: Path):
        proj = self.project
        if proj.direction is None:  # nothing made yet: save what's on screen
            try:
                proj.settings = self._collect()
            except ValueError as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))
                return
            proj.footage_paths, proj.song_path = self.footage_list.paths(), (self.song_list.paths() or [""])[0]
        try:
            saved = proj.save(path)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Couldn't save the project: {exc}")
            return
        self.setWindowTitle(f"{saved.stem} · {APP_NAME}")
        self.status.setText(f"Saved {saved.name}")

    def open_project(self):
        if self.job is not None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", str(self._projects_dir()),
                                              "Montage Director project (*.mdproj)")
        if path:
            self.load_project(Path(path))

    def load_project(self, path: Path):
        try:
            proj = Project.load(path, api_key=store.get_api_key())
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.new_project()
        self.project = proj
        s = proj.settings
        self.footage_list.add_paths(proj.footage_paths)
        if proj.song_path:
            self.song_list.add_paths([proj.song_path])
        self.prompt.setPlainText(s.prompt)
        self.styles.select(s.style)
        self.director.set_index(1 if s.use_ai else 0)
        for combo, value in ((self.shape, s.aspect), (self.fit, s.fit), (self.fps, s.fps)):
            i = combo.findData(value)
            if i >= 0:
                combo.setCurrentIndex(i)
        self.start_edit.setText(_mmss(s.song_start) if s.song_start else "")
        self.end_edit.setText(_mmss(s.song_end) if s.song_end else "")
        self.setWindowTitle(f"{path.stem} · {APP_NAME}")
        if proj.direction is None:
            self.status.setText(f"Opened {path.name}. Press Make Edit when you're ready.")
            return

        def work(cancel, progress):
            proj.analyse(cancel, lambda f, m: progress(0.4 * f, m))
            progress(0.42, "Planning cuts on the beat...")
            proj.make_plan()
            proj.render_preview(cancel, lambda f, m: progress(0.42 + 0.58 * f, m))
            return True

        self._run("make", work, lambda _: self._show_result())

    # ---------------------------------------------------------------- updates
    def check_for_updates(self):
        from ..updates import newer_release

        if self.saved.get("skip_update_checks"):
            return
        self._update_thread, self._update_job = start_job(
            self, lambda cancel, progress: newer_release(), self._ignore_progress, self._on_update_result,
            self._ignore_failure, self._ignore_cancel)

    def _ignore_progress(self, *_):
        pass

    def _ignore_failure(self, *_):
        pass

    def _ignore_cancel(self):
        pass

    def _on_update_result(self, result):
        if not result:
            return
        version, url = result
        if store.load().get("skipped_version") == version:
            return
        box = QMessageBox(QMessageBox.Information, APP_NAME,
                          f"Montage Director {version} is available (you have {__version__}).\n\n"
                          "Download and run the new installer to update. Your settings and "
                          "exports are kept.", parent=self)
        get = box.addButton("Download update", QMessageBox.AcceptRole)
        skip = box.addButton("Skip this version", QMessageBox.RejectRole)
        box.addButton("Later", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is get:
            QDesktopServices.openUrl(QUrl(url))
        elif box.clickedButton() is skip:
            store.save({**store.load(), "skipped_version": version})

    # ---------------------------------------------------------------- player
    def load_preview(self, path: Path | None):
        if not path:
            return
        if self.player is None:
            self.placeholder.setText(f"Preview saved:\n{path}\n\nUse \"Open in player\" to watch it.")
            return
        self.video_stack.setCurrentWidget(self.video_widget)
        tl = self.timeline
        if tl.zoomed():  # zoomed in on a part: play the new version from there
            self._seek_after_load = (int(max(0.0, tl.start - tl.full_a) * 1000), True)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def toggle_play(self):
        if not self.player:
            self.open_preview()
            return
        from PySide6.QtMultimedia import QMediaPlayer

        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek_seconds(self, seconds: float):
        if self.player:
            self.player.setPosition(int(seconds * 1000))

    def _on_position(self, ms: int):
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        dur = self.player.duration() if self.player else 0
        self.time_label.setText(f"{_mmss(ms / 1000)} / {_mmss(dur / 1000)}")
        self.timeline.set_position(ms / 1000)

    def open_preview(self):
        if self.project.preview_path and Path(self.project.preview_path).exists():
            open_in_system(Path(self.project.preview_path))

    def open_exports(self):
        folder = workspace_dir() / "Exports"
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def _setup_logging():
    handlers = [logging.FileHandler(data_dir() / "montage_director.log", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)


def main() -> int:
    _setup_logging()
    if sys.platform == "win32":
        try:  # own taskbar icon instead of python's
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MontageDirector.App")
        except Exception:
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    icon = ASSETS / ("icon.ico" if sys.platform == "win32" else "icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)
    win = MainWindow()
    win.show()
    return app.exec()
