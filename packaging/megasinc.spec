# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: pyinstaller packaging/megasinc.spec --noconfirm
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT))
from megasinc import __version__  # noqa: E402

APP_NAME = "Mega Sinc"
ICON = str(ROOT / "packaging" / ("icon.ico" if sys.platform == "win32" else "icon.png"))

binaries = []
ff_dir = ROOT / "vendor" / "ffmpeg"
if ff_dir.is_dir():
    for f in ff_dir.iterdir():
        if f.is_file() and f.name.lower().startswith(("ffmpeg", "ffprobe")):
            binaries.append((str(f), "ffmpeg"))

excludes = [
    "tkinter", "matplotlib", "scipy", "pandas", "IPython", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick", "PySide6.QtQml",
    "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSql", "PySide6.QtTest",
]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=[],
    hiddenimports=[],
    excludes=excludes,
    noarchive=False,
)
# Poda do Qt: o app usa só QtCore/QtGui/QtWidgets.
_DROP = ("qt6quick", "qtquick", "qt6qml", "qtqml", "qt6pdf", "qtpdf", "qt6virtualkeyboard",
         "qtvirtualkeyboard", "opengl32sw", "qt6opengl", "qmltooling", "qt6shadertools",
         "qtshadertools", "qt6labs", "networkinformation", "/tls/", "\\tls\\")


def _keep(entry) -> bool:
    dest = entry[0].replace("\\", "/").lower()
    if any(k.replace("\\", "/") in dest for k in _DROP):
        return False
    if "/translations/" in dest and not dest.endswith(("_pt_br.qm", "_pt.qm")):
        return False
    return True


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

pyz = PYZ(a.pure)

gui_exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="MegaSinc",
    console=False,
    icon=ICON,
    upx=False,
    target_arch=None,
)

exes = [gui_exe]
if sys.platform == "win32":
    # versão de linha de comando (console) na mesma pasta
    exes.append(EXE(pyz, a.scripts, [], exclude_binaries=True, name="megasinc-cli",
                    console=True, icon=ICON, upx=False))

coll = COLLECT(*exes, a.binaries, a.datas, strip=False, upx=False, name="MegaSinc")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=ICON,
        bundle_identifier="com.megasinc.app",
        version=__version__,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSRequiresAquaSystemAppearance": False,
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Projeto Mega Sinc",
                "CFBundleTypeExtensions": ["megasinc"],
                "CFBundleTypeRole": "Editor",
            }],
        },
    )
