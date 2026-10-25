"""Custom widgets: drop lists, style cards, segmented toggle and the edit timeline."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QGridLayout, QHBoxLayout, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QSizePolicy, QWidget)

from ..direction import STYLES
from . import theme


class DropList(QListWidget):
    """A list that accepts dragged files and folders."""

    changed = Signal()

    def __init__(self, extensions: set[str], single: bool = False, placeholder: str = ""):
        super().__init__()
        self.setObjectName("DropSingle" if single else "DropList")
        self.extensions = extensions
        self.single = single
        self.placeholder = placeholder
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        if single:
            self.setFixedHeight(38)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        else:
            self.setMaximumHeight(150)
        self.setToolTip(placeholder)

    def paths(self) -> list[str]:
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]

    def add_paths(self, paths) -> None:
        existing = {p.lower() for p in self.paths()}
        for raw in paths:
            p = Path(raw)
            ok = p.is_dir() if not self.single else False
            ok = ok or (p.is_file() and p.suffix.lower() in self.extensions)
            if not ok or str(p).lower() in existing:
                continue
            if self.single:
                self.clear()
            item = QListWidgetItem(("📁 " if p.is_dir() else "") + p.name)
            item.setData(Qt.UserRole, str(p))
            item.setToolTip(str(p))
            self.addItem(item)
            existing.add(str(p).lower())
            if self.single:
                break
        self.changed.emit()

    def remove_selected(self) -> None:
        for item in self.selectedItems():
            self.takeItem(self.row(item))
        self.changed.emit()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.count() == 0 and self.placeholder:
            p = QPainter(self.viewport())
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.viewport().rect().adjusted(8, 0, -8, 0), Qt.AlignCenter | Qt.TextWordWrap,
                       self.placeholder)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_paths(u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile())
        e.acceptProposedAction()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove_selected()
        else:
            super().keyPressEvent(e)


class StyleGrid(QWidget):
    """Two-column grid of style cards; exactly one is selected."""

    changed = Signal(str)

    def __init__(self, selected: str):
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        for i, style in enumerate(STYLES.values()):
            b = QPushButton(style.name)
            b.setObjectName("StyleCard")
            b.setCheckable(True)
            b.setToolTip(style.description)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(lambda _=False, sid=style.id: self.changed.emit(sid))
            self.group.addButton(b)
            self.buttons[style.id] = b
            grid.addWidget(b, i // 2, i % 2)
        self.select(selected)

    def select(self, style_id: str) -> None:
        if style_id in self.buttons:
            self.buttons[style_id].setChecked(True)

    def selected(self) -> str:
        for sid, b in self.buttons.items():
            if b.isChecked():
                return sid
        return next(iter(self.buttons))


class Segmented(QWidget):
    """A two-or-more option toggle that looks like one control."""

    changed = Signal(int)

    def __init__(self, labels: list[str], index: int = 0):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.group = QButtonGroup(self)
        for i, label in enumerate(labels):
            b = QPushButton(label)
            b.setObjectName("Seg")
            b.setCheckable(True)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.group.addButton(b, i)
            row.addWidget(b)
        self.group.button(index).setChecked(True)
        self.group.idClicked.connect(self.changed.emit)

    def index(self) -> int:
        return self.group.checkedId()

    def set_index(self, i: int) -> None:
        self.group.button(i).setChecked(True)


class Timeline(QWidget):
    """Song energy coloured by section, with cuts, flashes, drops and a playhead."""

    seek = Signal(float)  # seconds into the edit
    drops_changed = Signal(list)  # song seconds, after the user adds or removes a drop
    region_selected = Signal(float, float)  # song seconds; (0, 0) when the selection is cleared
    region_removed = Signal(float)          # song second inside a section edit to undo

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.song = None
        self.plan = None
        self.start = 0.0
        self.end = 1.0
        self.position = 0.0
        self.drops: list[float] | None = None  # None = the song analysis' drops
        self.selection: tuple[float, float] | None = None   # song seconds
        self.regions: list[tuple[float, float, str]] = []    # section edits: (start, end, label)
        self._press_x: float | None = None
        self._dragging = False
        self.setMouseTracking(True)
        self.setToolTip("Click to jump there. Drag across part of the song to refine just that part. "
                        "Right-click to mark a drop or undo a section edit.")

    def current_drops(self) -> list[float]:
        if self.drops is not None:
            return list(self.drops)
        return list(self.song.drops) if self.song else []

    def set_drops(self, drops: list[float] | None) -> None:
        self.drops = None if drops is None else sorted(drops)
        self.update()

    def _song_time(self, x: float) -> float:
        frac = min(1.0, max(0.0, x / max(1, self.width())))
        return self.start + frac * (self.end - self.start)

    def contextMenuEvent(self, e):
        if not self.song:
            return
        t = self._song_time(e.pos().x())
        beats = self.song.beats
        if beats:  # drops land on a beat
            t = min(beats, key=lambda b: abs(b - t))
        drops = self.current_drops()
        near = [d for d in drops if abs(self._x(d, self.width()) - e.pos().x()) < 8]
        menu = QMenu(self)
        add = menu.addAction(f"Mark the drop here ({int(t // 60)}:{t % 60:04.1f})")
        rem = menu.addAction("Remove this drop") if near else None
        reset = menu.addAction("Use the detected drops again") if self.drops is not None else None
        t_raw = self._song_time(e.pos().x())
        in_region = next((r for r in self.regions if r[0] <= t_raw < r[1]), None)
        undo = menu.addAction(f"Undo the section edit here ({in_region[2][:30]})") if in_region else None
        clear = menu.addAction("Clear the selection") if self.selection else None
        chosen = menu.exec(e.globalPos())
        if chosen is None:
            return
        if chosen is undo:
            self.region_removed.emit(t_raw)
            return
        if chosen is clear:
            self.clear_selection()
            return
        if chosen is add:
            drops = sorted(set(drops + [round(t, 3)]))
        elif chosen is rem:
            drops = [d for d in drops if d not in near]
        elif chosen is reset:
            self.set_drops(None)
            self.drops_changed.emit([])
            return
        self.set_drops(drops)
        self.drops_changed.emit(drops)

    def set_song(self, song, start: float = 0.0, end: float | None = None) -> None:
        self.song = song
        self.start = start
        self.end = end if end else (song.duration if song else 1.0)
        self.update()

    def set_plan(self, plan) -> None:
        self.plan = plan
        if plan and self.song:
            self.start = plan.song_start
            self.end = plan.song_start + plan.duration
        self.update()

    def set_position(self, seconds: float) -> None:
        self.position = seconds
        self.update()

    def _x(self, song_t: float, w: float) -> float:
        span = max(self.end - self.start, 1e-6)
        return (song_t - self.start) / span * w

    def set_regions(self, regions: list[tuple[float, float, str]]) -> None:
        self.regions = regions
        self.update()

    def clear_selection(self) -> None:
        if self.selection is not None:
            self.selection = None
            self.update()
            self.region_selected.emit(0.0, 0.0)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self.end <= self.start:
            return
        self._press_x, self._dragging = e.position().x(), False

    def mouseMoveEvent(self, e):
        if self._press_x is None or not self.song:
            return
        if not self._dragging and abs(e.position().x() - self._press_x) < 6:
            return
        self._dragging = True
        a, b = sorted((self._song_time(self._press_x), self._song_time(e.position().x())))
        self.selection = (a, b)
        self.update()

    def mouseReleaseEvent(self, e):
        if self._press_x is None:
            return
        if self._dragging and self.selection:
            a, b = self.selection
            beats = self.song.beats if self.song else []
            if beats:  # a selection starts and ends on a beat
                a = min(beats, key=lambda x: abs(x - a))
                b = min(beats, key=lambda x: abs(x - b))
            if b - a >= 0.4:
                self.selection = (a, b)
                self.region_selected.emit(a, b)
            else:
                self.clear_selection()
        else:
            frac = min(1.0, max(0.0, e.position().x() / max(1, self.width())))
            self.seek.emit(frac * (self.end - self.start))
        self._press_x, self._dragging = None, False
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(theme.PANEL))
        if not self.song:
            p.setPen(QColor(theme.MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "Your edit's timeline appears here")
            return
        top, wave_h = 18, h - 34
        song = self.song
        # Section bands + labels
        small = QFont(self.font())
        small.setPointSizeF(8)
        p.setFont(small)
        for s in song.sections:
            a, b = max(s.start, self.start), min(s.end, self.end)
            if b <= a:
                continue
            x0, x1 = self._x(a, w), self._x(b, w)
            col = QColor(theme.SECTION_COLORS.get(s.label, theme.ACCENT))
            band = QColor(col)
            band.setAlpha(38)
            p.fillRect(QRectF(x0, top, x1 - x0, wave_h), band)
            p.setPen(col)
            p.drawText(QRectF(x0 + 4, 1, max(0, x1 - x0 - 4), 16), Qt.AlignLeft | Qt.AlignVCenter,
                       s.label.upper())
        # Energy curve
        if song.energy:
            rate = song.energy_rate
            path = QPainterPath(QPointF(0, top + wave_h))
            steps = max(2, int(w / 2))
            for i in range(steps + 1):
                t = self.start + (self.end - self.start) * i / steps
                e = song.energy[min(len(song.energy) - 1, max(0, int(t * rate)))]
                path.lineTo(QPointF(w * i / steps, top + wave_h - e * (wave_h - 4)))
            path.lineTo(QPointF(w, top + wave_h))
            path.closeSubpath()
            fill = QColor(theme.ACCENT)
            fill.setAlpha(70)
            p.fillPath(path, fill)
        # Cuts, flashes
        if self.plan:
            fps = self.plan.fps
            pen = QPen(QColor(255, 255, 255, 60))
            pen.setWidthF(1)
            p.setPen(pen)
            for c in self.plan.cuts:
                x = self._x(self.plan.song_start + c.f0 / fps, w)
                p.drawLine(QPointF(x, top + wave_h - 10), QPointF(x, top + wave_h))
            for c in self.plan.cuts:
                for e in c.effects:
                    if e.kind in ("flash", "fade_white"):
                        x = self._x(self.plan.song_start + (c.f0 + e.frame) / fps, w)
                        p.fillRect(QRectF(x - 1, top, 2, 8), QColor("white"))
        # Drops
        p.setPen(QPen(QColor(theme.HOT), 2))
        for d in self.current_drops():
            if self.start <= d <= self.end:
                x = self._x(d, w)
                p.drawLine(QPointF(x, top), QPointF(x, top + wave_h))
        # Time ruler
        p.setPen(QColor(theme.MUTED))
        span = self.end - self.start
        step = 5 if span <= 60 else 15 if span <= 240 else 30
        t = (int(self.start) // step + 1) * step
        while t < self.end:
            x = self._x(t, w)
            p.drawText(QRectF(x - 20, h - 15, 40, 14), Qt.AlignCenter, f"{int(t // 60)}:{int(t % 60):02d}")
            t += step
        # What the app heard (events), as small labelled ticks along the top.
        marks = {"intro": "intro", "drums_faster": "drums↑", "stop": "stop", "riser": "riser", "solo": "solo",
                 "hits": "hits", "ringout": "ring", "fill": ""}
        tiny = QFont(self.font())
        tiny.setPointSizeF(7)
        p.setFont(tiny)
        for ev in song.events:
            if not (self.start <= ev["time"] <= self.end):
                continue
            x = self._x(ev["time"], w)
            col = QColor(theme.WARN if ev["kind"] != "fill" else theme.MUTED)
            p.setPen(QPen(col, 1))
            p.drawLine(QPointF(x, top), QPointF(x, top + (6 if ev["kind"] == "fill" else 12)))
            label = marks.get(ev["kind"], "")
            if label:
                p.drawText(QRectF(x + 2, top + 1, 60, 12), Qt.AlignLeft | Qt.AlignVCenter, label)
        # Section edits the user made, and the current selection.
        for a, b, label in self.regions:
            x0, x1 = self._x(max(a, self.start), w), self._x(min(b, self.end), w)
            p.setPen(QPen(QColor(theme.OK), 1.5))
            p.setBrush(QColor(69, 212, 131, 40))
            p.drawRect(QRectF(x0, top, max(2, x1 - x0), wave_h))
            p.drawText(QRectF(x0 + 3, top + wave_h - 14, max(0, x1 - x0 - 4), 12), Qt.AlignLeft | Qt.AlignVCenter,
                       label)
        if self.selection:
            a, b = self.selection
            x0, x1 = self._x(a, w), self._x(b, w)
            p.setPen(QPen(QColor(theme.ACCENT_HI), 2))
            p.setBrush(QColor(139, 108, 255, 70))
            p.drawRect(QRectF(x0, top - 1, max(2, x1 - x0), wave_h + 2))
        p.setBrush(Qt.NoBrush)
        p.setFont(small)
        # Playhead
        if self.plan:
            x = self._x(self.start + self.position, w)
            p.setPen(QPen(QColor(theme.TEXT), 2))
            p.drawLine(QPointF(x, top - 2), QPointF(x, top + wave_h + 2))
