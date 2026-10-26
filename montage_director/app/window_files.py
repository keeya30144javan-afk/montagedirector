"""MainWindow: new / open / save / recent projects, unsaved-changes prompts, autosave and crash recovery."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..paths import APP_NAME, data_dir, workspace_dir
from ..project import Project
from . import store
from .common import _mmss

log = logging.getLogger(__name__)

class ProjectFilesMixin:
    """Part of MainWindow (see main_window.py). New / open / save / recent projects, unsaved-changes prompts,
    autosave and crash recovery."""

    def _set_dirty(self, dirty: bool):
        self.dirty = dirty
        name = self.project.path.stem if self.project.path else ""
        mark = " •" if dirty and (name or self.project.plan is not None) else ""
        self.setWindowTitle(f"{name or 'Unsaved project'}{mark} · {APP_NAME}" if (name or mark) else APP_NAME)
        if hasattr(self, "project_label"):
            self.project_label.setText((name or "Unsaved project") + (" (unsaved changes)" if mark else ""))


    def maybe_save(self) -> bool:
        """Ask to save unsaved changes. False means the user cancelled."""
        if not self.dirty or self.project.plan is None:
            return True
        box = QMessageBox(QMessageBox.Question, APP_NAME, "Save the changes to this project first?", parent=self)
        save = box.addButton("Save", QMessageBox.AcceptRole)
        discard = box.addButton("Don't save", QMessageBox.DestructiveRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is save:
            self.save_project()
            return not self.dirty
        return box.clickedButton() is discard


    def _remember_recent(self, path: Path):
        data = store.load()
        recent = [p for p in data.get("recent_projects", []) if p != str(path)]
        store.save({**data, "recent_projects": [str(path)] + recent[:9]})


    def _fill_recent(self):
        self.recent_menu.clear()
        paths = [p for p in store.load().get("recent_projects", []) if Path(p).is_file()]
        if not paths:
            a = self.recent_menu.addAction("(no recent projects yet)")
            a.setEnabled(False)
            return
        for p in paths:
            a = self.recent_menu.addAction(Path(p).stem)
            a.setToolTip(p)
            a.triggered.connect(lambda _=False, p=p: self.open_recent(Path(p)))
        self.recent_menu.addSeparator()
        self.recent_menu.addAction("Clear this list").triggered.connect(
            lambda: store.save({**store.load(), "recent_projects": []}))


    def open_recent(self, path: Path):
        if self.job is not None or not self.maybe_save():
            return
        self.load_project(path)


    @staticmethod
    def _fresh_project() -> Project:
        from ..project import DEFAULT_SCENE_KEYS, SCENE_PRESETS

        proj = Project()
        saved = store.load().get("scene_keys") or {}
        proj.scene_keys = {**DEFAULT_SCENE_KEYS, **{k: v for k, v in saved.items() if v in SCENE_PRESETS}}
        return proj


    @staticmethod
    def _recovery_path() -> Path:
        return data_dir() / "recovery.mdproj"


    def _autosave(self):
        if self.dirty and self.project.plan is not None and self.job is None:
            try:
                self.project.save_copy(self._recovery_path())
            except OSError as exc:
                log.warning("Autosave failed: %s", exc)


    def _offer_recovery(self):
        path = self._recovery_path()
        if not path.is_file():
            return
        box = QMessageBox(QMessageBox.Question, APP_NAME,
                          "Montage Director closed before your last edit was saved. Bring it back?", parent=self)
        restore = box.addButton("Restore my edit", QMessageBox.AcceptRole)
        box.addButton("Discard it", QMessageBox.DestructiveRole)
        box.exec()
        if box.clickedButton() is restore:
            self.load_project(path, recovered=True)
        else:
            path.unlink(missing_ok=True)


    def ask_new_project(self):
        if self.job is not None or not self.maybe_save():
            return
        self.new_project()


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
        self.project = self._fresh_project()
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
        self._set_dirty(False)
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
        self._remember_recent(saved)
        self._set_dirty(False)
        self.status.setText(f"Saved {saved.name}")


    def open_project(self):
        if self.job is not None or not self.maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", str(self._projects_dir()),
                                              "Montage Director project (*.mdproj)")
        if path:
            self.load_project(Path(path))


    def load_project(self, path: Path, recovered: bool = False):
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
        if recovered:  # back where it was saved (or unsaved), with its changes still to save
            self._just_opened = False
            self._recovery_path().unlink(missing_ok=True)
        else:
            self._remember_recent(path)
            self._just_opened = True
        self._set_dirty(recovered)
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
