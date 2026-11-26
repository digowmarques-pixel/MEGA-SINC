"""Gera packaging/icon.png (1024 px) e packaging/icon.ico a partir do logo do app."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from megasinc.gui.app import draw_logo  # noqa: E402

app = QGuiApplication(sys.argv)
png = ROOT / "packaging" / "icon.png"
draw_logo(1024).save(str(png))
from PIL import Image  # noqa: E402

Image.open(png).save(ROOT / "packaging" / "icon.ico",
                     sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ícones gerados em", png.parent)
