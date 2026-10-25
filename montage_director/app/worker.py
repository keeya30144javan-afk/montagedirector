"""Run pipeline steps off the UI thread and report progress back with signals."""

from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QObject, QThread, Signal


class Job(QObject):
    progress = Signal(float, str)
    done = Signal(object)
    failed = Signal(str, str)      # message, details
    cancelled = Signal()

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.cancel = threading.Event()

    def run(self):
        from ..director_ai import AIDirectorError
        from ..ffmpeg_tools import Cancelled, MediaError

        try:
            result = self.fn(self.cancel, lambda f, m: self.progress.emit(float(f), str(m)))
        except Cancelled:
            self.cancelled.emit()
        except (MediaError, AIDirectorError, ValueError) as exc:
            if self.cancel.is_set():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc), "")
        except Exception as exc:  # unexpected: show details so users can report it
            if self.cancel.is_set():
                self.cancelled.emit()
            else:
                self.failed.emit(f"Something went wrong: {exc}", traceback.format_exc())
        else:
            self.done.emit(result)


def start_job(parent: QObject, fn, on_progress, on_done, on_failed, on_cancelled) -> tuple[QThread, Job]:
    thread = QThread(parent)
    job = Job(fn)
    job.moveToThread(thread)
    thread.started.connect(job.run)
    job.progress.connect(on_progress)
    job.done.connect(on_done)
    job.failed.connect(on_failed)
    job.cancelled.connect(on_cancelled)
    for sig in (job.done, job.failed, job.cancelled):
        sig.connect(thread.quit)
    thread.finished.connect(job.deleteLater)
    thread.start()
    return thread, job
