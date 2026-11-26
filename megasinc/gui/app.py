"""Ponto de entrada da interface gráfica."""
from __future__ import annotations

import sys


def draw_logo(size: int = 1024):
    """Ícone Mega Sinc: barras de áudio amarelas alinhadas por uma linha de sincronia."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    k = size / 256.0
    path = QPainterPath()
    path.addRoundedRect(QRectF(8 * k, 8 * k, 240 * k, 240 * k), 54 * k, 54 * k)
    p.fillPath(path, QColor("#121212"))
    p.setPen(QPen(QColor("#FFC400"), 5 * k))
    p.drawPath(path)
    rows = [([40, 92, 60, 124, 72, 104, 50], "#FFC400"),
            ([70, 50, 112, 82, 132, 62, 92], "#FFE27A"),
            ([56, 100, 76, 60, 116, 86, 46], "#FFC400")]
    for r, (hs, col) in enumerate(rows):
        y = (72 + r * 56) * k
        p.setPen(QPen(QColor(col), 13 * k, Qt.SolidLine, Qt.RoundCap))
        for i, h in enumerate(hs):
            x = (50 + i * 26) * k
            p.drawLine(QPointF(x, y - h / 6 * k), QPointF(x, y + h / 6 * k))
    p.setPen(QPen(QColor("#FFFFFF"), 6 * k, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(128 * k, 36 * k), QPointF(128 * k, 220 * k))
    p.end()
    return pm


def _app_icon():
    from PySide6.QtGui import QIcon
    return QIcon(draw_logo(256))


def run_gui() -> int:
    from PySide6.QtWidgets import QApplication

    from .. import APP_NAME
    from .main_window import MainWindow, apply_dark_theme

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("MegaSinc")
    apply_dark_theme(app)
    app.setWindowIcon(_app_icon())
    w = MainWindow()
    w.show()
    # arquivos passados na linha de comando (ou "Abrir com…")
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    projects = [a for a in args if a.lower().endswith(".megasinc")]
    media = [a for a in args if a not in projects]
    if projects:
        w.load_project(projects[0])
    elif media:
        w.add_paths(media)
    return app.exec()


if __name__ == "__main__":
    sys.exit(run_gui())
