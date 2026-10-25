# PyInstaller build recipe: `pyinstaller MontageDirector.spec`
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [("montage_director/assets", "montage_director/assets")]
datas += collect_data_files("librosa")          # lazy-loader stubs and example registry
datas += collect_data_files("imageio_ffmpeg")   # the bundled ffmpeg binary
hiddenimports = collect_submodules("librosa") + ["sklearn.utils._typedefs", "keyring.backends"]

a = Analysis(
    ["run_app.py"],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PySide6.Qt3DCore", "PySide6.QtWebEngineCore",
              "PySide6.QtWebEngineWidgets", "PySide6.QtQuick3D", "PySide6.QtCharts", "PySide6.QtDataVisualization"],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = "montage_director/assets/icon.ico" if sys.platform == "win32" else "montage_director/assets/icon_1024.png"
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MontageDirector", console=False, icon=icon)
coll = COLLECT(exe, a.binaries, a.datas, name="MontageDirector")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Montage Director.app", icon="montage_director/assets/icon_1024.png",
                 bundle_identifier="app.montagedirector",
                 info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": "0.1.0"})
