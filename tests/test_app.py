"""The app window builds and all its parts (layout, files, timeline, preview) fit together."""

import os

import pytest

os.environ["QT_QPA_PLATFORM"] = os.environ.get("QT_QPA_PLATFORM") or "offscreen"
QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)


def test_the_window_builds(tmp_path, monkeypatch):
    monkeypatch.setenv("MONTAGE_DATA_DIR", str(tmp_path))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from montage_director.app.main_window import MainWindow

    w = MainWindow()
    for name in ("_build_menus", "maybe_save", "load_project", "toggle_tapping", "on_tap_key", "toggle_play",
                 "toggle_compare", "make_edit", "refine", "export"):
        assert callable(getattr(w, name)), name
    w._set_dirty(False)
    assert w.windowTitle().endswith("Montage Director") and not w.dirty
    app.processEvents()
