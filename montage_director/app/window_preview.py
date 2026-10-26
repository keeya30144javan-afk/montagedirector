"""MainWindow: the video preview: loading, play/pause, seeking, before/after compare."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from ..paths import workspace_dir
from .common import _mmss, open_in_system


class PreviewMixin:
    """Part of MainWindow (see main_window.py). The video preview: loading, play/pause, seeking, before/after
    compare."""

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

        if not self._seek_after_load:
            return
        if status == QMediaPlayer.LoadingMedia:
            self._seek_armed = True  # the NEW file is loading (the old one also reports "loaded" as it closes)
            return
        if getattr(self, "_seek_armed", False) and status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self._seek_armed = False
            pos, playing = self._seek_after_load
            self._seek_after_load = None
            self.player.setPosition(pos)
            if playing:
                self.player.play()


    def load_preview(self, path: Path | None):
        if not path:
            return
        if self.player is None:
            self.placeholder.setText(f"Preview saved:\n{path}\n\nUse \"Open in player\" to watch it.")
            return
        self.video_stack.setCurrentWidget(self.video_widget)
        tl = self.timeline
        focus, self._focus_at = getattr(self, "_focus_at", None), None
        if focus is not None and self.project.plan is not None:  # just changed a part: play from there
            self._seek_after_load = (int(max(0.0, focus - self.project.plan.song_start - 0.5) * 1000), True)
        elif tl.zoomed():  # zoomed in on a part: play the new version from there
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
