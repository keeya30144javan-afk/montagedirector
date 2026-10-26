"""MainWindow: everything on the timeline: Tap to Direct, selected parts (regions), markers, drops,
picking shots, text cards and scene keys."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from ..paths import APP_NAME
from . import store, theme
from .common import TAP_HELP, TAP_KEYS, _mmss
from .dialogs import InstructionDialog, SceneKeys, ShotPicker, TextCards


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


class TimelineMixin:
    """Part of MainWindow (see main_window.py). Everything on the timeline: Tap to Direct, selected parts
    (regions), markers, drops, picking shots, text cards and scene keys."""

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
        before = proj.snapshot()
        dlg = ShotPicker(self, proj, b - a)
        if dlg.exec() != QDialog.Accepted or dlg.chosen is None:
            if dlg.marks_changed:
                self._marks_changed(before)
            return
        proj.history.append(before)
        proj.future.clear()
        proj.pick_region_shot(a, b, dlg.chosen, dlg.freeze.isChecked() and len(dlg.chosen) == 1)
        self.timeline.clear_selection()
        what = "your shot" if len(dlg.chosen) == 1 else f"your {len(dlg.chosen)} shots"
        self._replan(f"Putting {what} in {_mmss(a)}–{_mmss(b)}...")


    def _marks_changed(self, before: dict):
        proj = self.project
        proj.history.append(before)
        proj.future.clear()
        if proj.plan is not None:
            self._replan("Updating your ★ / ✕ shots...")


    def browse_shots(self):
        proj = self.project
        if proj.footage is None:
            QMessageBox.information(self, APP_NAME, "Make an edit first, so the app has looked at your footage.")
            return
        if self.job is not None:
            return
        before = proj.snapshot()
        dlg = ShotPicker(self, proj)
        dlg.exec()
        if dlg.marks_changed:
            self._marks_changed(before)


    def describe_shots(self):
        proj = self.project
        if self.job is not None:
            return
        if proj.footage is None:
            QMessageBox.information(self, APP_NAME, "Press Make Edit first (the app needs to look at your footage once), "
                                    "then Describe my shots.")
            return
        key = store.get_api_key()
        if not key:
            QMessageBox.information(self, APP_NAME, "Describing your shots uses Claude, so it needs your Anthropic API "
                                    "key (Settings). The rest of the app stays free.")
            self.open_settings()
            return
        from ..director_ai import describe_cost

        todo, cost = describe_cost(proj.footage)
        if todo == 0:
            QMessageBox.information(self, APP_NAME, "Your shots are already described.")
            return
        box = QMessageBox(QMessageBox.Question, APP_NAME,
                          f"Claude (Haiku, the cheapest model) will look at {todo} shots once and note who is in "
                          f"each, what happens and how many characters there are. Estimated cost: about "
                          f"${max(cost, 0.01):.2f}. It's saved, so this video never needs it again.", parent=self)
        go = box.addButton("Describe my shots", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not go:
            return
        proj.settings.api_key = key

        def work(cancel, progress):
            n = proj.describe_shots(cancel, progress)
            if proj.plan is not None:
                progress(0.9, "Re-planning with the shot descriptions...")
                proj.make_plan()
                proj.render_preview(cancel, lambda f, m: progress(0.9 + 0.1 * f, m))
            return n

        def done(n):
            self.status.setText(f"Described {n} shots. You can now ask for characters by name or "
                                "\"as many characters as possible\".")
            if proj.plan is not None:
                self._show_result()

        self._run("refine", work, done)


    def edit_texts(self):
        proj = self.project
        length = proj.plan.duration if proj.plan else 0.0
        dlg = TextCards(self, proj.texts, length, proj.plan.song_start if proj.plan else 0.0)
        if dlg.exec() != QDialog.Accepted or dlg.texts == proj.texts:
            return
        proj.checkpoint()
        proj.texts = dlg.texts
        if proj.plan is not None:
            self._replan("Adding your text...")
        else:
            self._set_dirty(True)


    def edit_scene_keys(self):
        dlg = SceneKeys(self, self.project.scene_keys)
        if dlg.exec() != QDialog.Accepted:
            return
        self.project.scene_keys = dlg.keys()
        store.save({**store.load(), "scene_keys": self.project.scene_keys})
        if self.project.plan is not None and any(k.startswith("scene") for _, k in self.project.taps):
            self.project.checkpoint()
            self.project.apply_taps()
            self._replan("Updating your scene taps...")


    def move_tap(self, old: float, new: float, exact: bool):
        if self.job is not None or self.tapping:
            return
        self.project.checkpoint()
        if not self.project.move_tap(old, new, exact):
            self.project.history.pop()
            return
        self.timeline.set_taps(self.project.taps)
        self._replan("Moving your tap" + (" (exactly where you put it)..." if exact else "..."))


    def insert_instruction(self, t: float):
        proj = self.project
        if proj.plan is None or self.job is not None:
            QMessageBox.information(self, APP_NAME, "Make an edit first.")
            return
        plan, song = proj.plan, proj.song
        S, E = plan.song_start, plan.song_start + plan.duration
        beat = 60.0 / song.tempo if song.tempo else 0.5
        sec = song.section_at(t)
        choices = []
        sel = self.timeline.selection
        if sel and sel[1] - sel[0] >= 0.4:
            choices.append((f"The selected part ({_mmss(sel[0])}–{_mmss(sel[1])})", sel[0], sel[1]))
        a_sec, b_sec = max(S, sec.start), min(E, sec.end)
        choices.append((f"This part of the song: {sec.label} ({_mmss(a_sec)}–{_mmss(b_sec)})", a_sec, b_sec))
        choices.append((f"From here to the end ({_mmss(t)}–{_mmss(E)})", t, E))
        choices.append((f"The next 4 bars ({_mmss(t)}–{_mmss(min(E, t + 16 * beat))})", t, min(E, t + 16 * beat)))
        choices.append(("Just this moment (a flash, a freeze, a text...)", t, min(E, t + beat)))
        here = 1 if sel else 0  # index of "This part of the song"
        default = 0 if sel else (here + 1 if t >= S + 0.85 * (E - S) else here)
        dlg = InstructionDialog(self, t, choices, default, helper=self.typing_help)
        if dlg.exec() != QDialog.Accepted:
            return
        text = dlg.text.toPlainText().strip()
        if not text:
            return
        a, b = dlg.scope.currentData()
        self.refine_part(text, a, b)


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
        self.tap_rate = 0.5 if self.half_speed.isChecked() else 1.0
        self.player.setPlaybackRate(self.tap_rate)
        self.player.play()


    def stop_tapping(self, apply: bool):
        self.tapping = False
        QApplication.instance().removeEventFilter(self.tap_filter)
        self.tap_btn.setText("Tap to Direct")
        self.tap_btn.setStyleSheet("")
        if self.player:
            self.player.pause()
            self.player.setPlaybackRate(1.0)
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
            # (at half speed, the same reaction time is half as much song)
            t = plan.song_start + max(0.0, self.player.position() / 1000.0 - 0.06 * getattr(self, "tap_rate", 1.0))
            taps.append((round(t, 3), TAP_KEYS[key]))  # kept in pressing order, so Backspace undoes the last press
        self.timeline.set_taps(taps)
        counts = {k: sum(1 for _, kind in taps if kind == k or (k == "scene" and kind.startswith("scene")))
                  for k in ("cut", "scene", "flash", "zoom", "shake", "drop")}
        summary = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in counts.items() if n)
        self.status.setText(f"{TAP_HELP}   [{summary or 'no taps yet'}]")


    def _timeline_view(self, a: float, b: float):
        tl = self.timeline
        full = max(tl.full_b - tl.full_a, 1e-6)
        self.timeline_bar.blockSignals(True)
        self.timeline_bar.setRange(0, int(round((full - (b - a)) * 100)))
        self.timeline_bar.setPageStep(max(1, int((b - a) * 100)))
        self.timeline_bar.setValue(int(round((a - tl.full_a) * 100)))
        self.timeline_bar.setEnabled(tl.zoomed())
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


    def refine_part(self, text: str, a: float, b: float):
        """Change one part of the song (a selection, or Insert instruction here)."""
        proj = self.project
        proj.checkpoint()

        def work_region(cancel, progress):
            progress(0.05, f"Refining {_mmss(a)}–{_mmss(b)}...")
            notes = proj.refine_region(text, a, b)
            proj.make_plan()
            proj.render_preview(cancel, lambda f, m: progress(0.15 + 0.85 * f, m))
            return notes

        def done_region(notes):
            self.refine_edit.clear()
            self.timeline.clear_selection()
            self.region_selected(0.0, 0.0)
            self._focus_at = a  # play the new version from the part that changed
            self._show_result()
            self.status.setText(f"Changed {_mmss(a)}–{_mmss(b)}: " + "; ".join(n.split(": ", 1)[-1] for n in notes)
                                + "  (playing from there)")

        self._run("refine", work_region, done_region)


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
