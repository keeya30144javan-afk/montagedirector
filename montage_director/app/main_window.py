"""Montage Director main window."""

from __future__ import annotations

import html
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QMainWindow, QMessageBox, QWidget

from .. import __version__
from ..director_ai import MODELS
from ..footage import VIDEO_EXTENSIONS
from ..music import AUDIO_EXTENSIONS
from ..paths import APP_NAME, data_dir
from ..project import Settings
from . import store, theme
from .common import ASSETS, PROMPT_HELP, _mmss, _parse_time, open_in_system
from .dialogs import SettingsDialog
from .window_files import ProjectFilesMixin
from .window_layout import LayoutMixin
from .window_preview import PreviewMixin
from .window_timeline import TimelineMixin
from .worker import start_job

log = logging.getLogger(__name__)


class MainWindow(LayoutMixin, ProjectFilesMixin, TimelineMixin, PreviewMixin, QMainWindow):
    """The app window. Its parts live in window_layout.py, window_files.py, window_timeline.py and
    window_preview.py; this file keeps start-up, running the jobs (make, refine, export) and help."""

    REFINE_HINT = ("Tell it what to change, e.g. \"more flashes in the drop\". Or drag across the timeline "
                   "to change just that part.")

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1360, 860)
        self.setMinimumSize(QSize(1060, 680))
        self.saved = store.load()
        self.project = self._fresh_project()
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
        self.autosave = QTimer(self)
        self.autosave.timeout.connect(self._autosave)
        self.autosave.start(120_000)
        QTimer.singleShot(400, self._offer_recovery)
        self._update_director_info()
        self._set_busy(False)
        QTimer.singleShot(3000, self.check_for_updates)

    # ---------------------------------------------------------------- help menu

    def show_about(self):
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<b>{APP_NAME} {__version__}</b><br><br>Make AMVs, GMVs and montages that hit the beat.<br>"
                          "<a href='https://github.com/keeya30144javan-afk/montagedirector'>"
                          "github.com/keeya30144javan-afk/montagedirector</a>")

    def check_for_updates_now(self):
        from ..updates import newer_release

        self._manual_update_check = True
        self._update_thread, self._update_job = start_job(
            self, lambda cancel, progress: newer_release(), self._ignore_progress, self._on_update_result,
            self._ignore_failure, self._ignore_cancel)

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
        if not self.maybe_save():
            e.ignore()
            return
        self._recovery_path().unlink(missing_ok=True)  # closed properly: nothing to recover
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

    # ------------------------------------------------------- replan / refine

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
        region = getattr(self, "region", None)
        if region:
            self.refine_part(text, *region)
            return
        proj = self.project
        proj.checkpoint()

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
        if not proj.settings.use_ai:
            from ..direction import style_direction
            from ..reader import did_you_mean

            fixes = did_you_mean(proj.settings.prompt, style_direction(proj.settings.style))
            if fixes:
                lines.append(f"<span style='color:{theme.WARN}'>Did you mean " + ", ".join(
                    f"“{html.escape(good)}” (not “{html.escape(bad)}”)" for bad, good in fixes) + "?</span>")
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
        self._set_dirty(not getattr(self, "_just_opened", False))
        self._just_opened = False

    # ----- the speech bubbles: how what you type will be read -------------------------- #
    def typing_help(self, text: str, mode: str):
        from ..reader import typing_help

        try:
            return typing_help(text, mode, has_tags=bool(self.project.shot_tags),
                               use_ai=self.director.index() == 1, style=self.styles.selected())
        except Exception as exc:  # never let a hint break typing
            log.debug("typing help failed: %s", exc)
            return [], []

    def _prompt_help(self):
        self.prompt_bubble.show_help(*self.typing_help(self.prompt.toPlainText(), "idea"))

    def _refine_help(self):
        mode = "part" if getattr(self, "region", None) else "refine"
        self.refine_bubble.show_help(*self.typing_help(self.refine_edit.text(), mode))

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
        manual, self._manual_update_check = getattr(self, "_manual_update_check", False), False
        if not result:
            if manual:
                QMessageBox.information(self, APP_NAME, f"You have the latest version ({__version__}).")
            return
        version, url = result
        if store.load().get("skipped_version") == version and not manual:
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
