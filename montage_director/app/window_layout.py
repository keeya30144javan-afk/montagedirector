"""MainWindow: builds the window: menus, toolbar, the sidebar (inputs and settings) and the main area
(preview, timeline, refine box)."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QSlider,
    QStackedLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..direction import STYLES
from ..footage import VIDEO_EXTENSIONS
from ..music import AUDIO_EXTENSIONS
from ..paths import APP_NAME
from ..project import EXPORT_PRESETS
from . import theme
from .common import FITS, FPS, IDEAS, PROMPT_HINT, SHAPES, section_title
from .widgets import DropList, Segmented, StyleGrid, Timeline, TipBubble
from .window_timeline import TapFilter

log = logging.getLogger(__name__)

class LayoutMixin:
    """Part of MainWindow (see main_window.py). Builds the window: menus, toolbar, the sidebar (inputs and
    settings) and the main area (preview, timeline, refine box)."""

    def _build_menus(self):
        def act(text, slot, keys=(), tip=""):
            a = QAction(text, self)
            a.setShortcuts([QKeySequence(k) for k in keys])
            if tip:
                a.setToolTip(tip)
            a.triggered.connect(slot)
            return a

        self.new_action = act("New project", self.ask_new_project, ["Ctrl+N"], "Start a new project (Ctrl+N)")
        self.open_action = act("Open project...", self.open_project, [QKeySequence.Open], "Open a saved project (Ctrl+O)")
        self.save_action = act("Save project", self.save_project, [QKeySequence.Save], "Save this project (Ctrl+S)")
        self.save_as_action = act("Save project as...", self.save_project_as, ["Ctrl+Shift+S"])
        self.export_action = act("Export video...", self.export, ["Ctrl+E"], "Export the finished video (Ctrl+E)")
        self.undo_action = act("Undo change", self.undo, [QKeySequence.Undo], "Undo the last change (Ctrl+Z)")
        self.redo_action = act("Redo change", self.redo, ["Ctrl+Y", "Ctrl+Shift+Z"], "Redo (Ctrl+Y)")
        for a, short in ((self.new_action, "New"), (self.open_action, "Open"), (self.save_action, "Save"),
                         (self.save_as_action, "Save as"), (self.export_action, "Export video"),
                         (self.undo_action, "↶ Undo"), (self.redo_action, "↷ Redo")):
            a.setIconText(short)  # the toolbar's shorter label
        self.recent_menu = QMenu("Open recent", self)
        self.recent_menu.aboutToShow.connect(self._fill_recent)

        file = self.menuBar().addMenu("&File")
        file.addAction(self.new_action)
        file.addAction(self.open_action)
        file.addMenu(self.recent_menu)
        file.addSeparator()
        file.addAction(self.save_action)
        file.addAction(self.save_as_action)
        file.addSeparator()
        file.addAction(self.export_action)
        file.addAction(act("Open the exports folder", self.open_exports))
        file.addSeparator()
        file.addAction(act("Quit", self.close, ["Ctrl+Q"]))
        edit = self.menuBar().addMenu("&Edit")
        edit.addAction(self.undo_action)
        edit.addAction(self.redo_action)
        edit.addSeparator()
        edit.addAction(act("Compare before / after", lambda: self.compare_btn.isEnabled() and self.compare_btn.toggle(),
                           ["C"]))
        edit.addAction(act("Settings...", self.open_settings))
        tools = self.menuBar().addMenu("&Tools")
        self.shots_action = act("Your shots (★ favourites / ✕ never use)...", self.browse_shots, [], "Browse and mark your shots")
        self.text_action = act("Text cards (titles, credits)...", self.edit_texts, ["Ctrl+T"], "Add titles, captions or credits")
        tools.addAction(self.shots_action)
        tools.addAction(self.text_action)
        tools.addAction(act("Describe my shots (AI, about a cent)...", self.describe_shots))
        tools.addAction(act("Scene keys for tapping...", self.edit_scene_keys))
        self.shots_action.setIconText("Shots")
        self.text_action.setIconText("Text")
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(act("What can I say? (prompt guide)", self.show_prompt_help, ["F1"]))
        help_menu.addAction(act("How to make an edit (online guide)", lambda: QDesktopServices.openUrl(
            QUrl("https://github.com/keeya30144javan-afk/montagedirector#how-to-make-an-edit"))))
        help_menu.addAction(act("Check for updates", self.check_for_updates_now))
        help_menu.addSeparator()
        help_menu.addAction(act(f"About {APP_NAME}", self.show_about))

        bar = self.addToolBar("Project")
        bar.setObjectName("ProjectBar")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        for a in (self.new_action, self.open_action):
            bar.addAction(a)
        recent = QToolButton()
        recent.setText("Recent ▾")
        recent.setToolTip("Open a project you used recently")
        recent.setMenu(self.recent_menu)
        recent.setPopupMode(QToolButton.InstantPopup)
        bar.addWidget(recent)
        bar.addAction(self.save_action)
        bar.addAction(self.save_as_action)
        bar.addSeparator()
        bar.addAction(self.undo_action)
        bar.addAction(self.redo_action)
        bar.addSeparator()
        bar.addAction(self.shots_action)
        bar.addAction(self.text_action)
        bar.addSeparator()
        bar.addAction(self.export_action)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)
        self.project_label = QLabel("Unsaved project")
        self.project_label.setObjectName("ProjectName")
        bar.addWidget(self.project_label)
        self.dirty = False


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
        row = QHBoxLayout()
        self.describe_btn = QPushButton("Describe my shots (AI)…")
        self.describe_btn.setToolTip("Claude looks at every shot once (who is in it, what happens, how many characters) "
                                     "so the free director can find shots by name or pick the one with the most "
                                     "characters. Uses your API key with the cheapest model: about a cent per video, "
                                     "saved so it never runs twice.")
        self.describe_btn.clicked.connect(self.describe_shots)
        row.addWidget(self.describe_btn)
        shots_btn = QPushButton("Your shots…")
        shots_btn.setToolTip("Browse your shots, search them, mark ★ favourites or ✕ never use")
        shots_btn.clicked.connect(self.browse_shots)
        row.addWidget(shots_btn)
        v.addLayout(row)
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
        self.prompt_bubble = TipBubble()
        v.addWidget(self.prompt_bubble)
        self._prompt_timer = QTimer(self)
        self._prompt_timer.setSingleShot(True)
        self._prompt_timer.timeout.connect(self._prompt_help)
        self.prompt.textChanged.connect(lambda: self._prompt_timer.start(400))
        self.styles.changed.connect(lambda _: self._prompt_timer.start(100))
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
        self.director.changed.connect(lambda _: (self._update_director_info(), self._prompt_timer.start(100)))
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
        self.video_frame.setStyleSheet("background: black; border-radius: 8px;")
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
        self.half_speed = QCheckBox("½ speed")
        self.half_speed.setToolTip("Tap along at half speed: easier to catch fast drum fills. Your taps still land "
                                   "in the right place at full speed.")
        controls.addWidget(self.half_speed)
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
        self.timeline.tap_moved.connect(self.move_tap)
        self.timeline.instruction_requested.connect(self.insert_instruction)
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
        self.refine_bubble = TipBubble()
        v.addWidget(self.refine_bubble)
        self._refine_timer = QTimer(self)
        self._refine_timer.setSingleShot(True)
        self._refine_timer.timeout.connect(self._refine_help)
        self.refine_edit.textChanged.connect(lambda: self._refine_timer.start(350))

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
