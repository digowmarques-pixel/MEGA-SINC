"""Visualização da timeline sincronizada."""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QScrollArea, QToolTip, QWidget

from ..media import seconds_to_tc
from ..project import Project
from ..timeline import Timeline, build_timeline

LABEL_W = 150
RULER_H = 26
ROW_H = 30
ROW_GAP = 4

# Paleta Mega Sinc: tons escuros + amarelo
YELLOW = QColor("#FFC400")
C_BG = QColor("#111111")
C_ROW = QColor("#1a1a1a")
C_ROW_ALT = QColor("#1e1e1e")
C_RULER = QColor("#222222")
C_LABELS = QColor("#161616")
C_TEXT = QColor("#ECECEC")
C_DIM = QColor("#8E8E8E")
C_VIDEO = QColor("#FFC400")     # câmeras: amarelo
C_AUDIO = QColor("#5B5F66")     # gravadores: grafite
C_REF = QColor("#FFE27A")       # referência: amarelo claro
C_SEL = QColor("#FFFFFF")
C_FAIL = QColor("#FF4D4D")      # não sincronizado / erro
FAIL_ROW = "✖ Não sincronizados"


class TimelineCanvas(QWidget):
    clipClicked = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.tl: Optional[Timeline] = None
        self.ref_uid: Optional[int] = None
        self.px_per_s = 4.0
        self.selected: set[int] = set()
        self._rows: list[tuple[str, int, bool]] = []    # (faixa, lane, vídeo?)
        self._rects: list[tuple[QRectF, object]] = []
        self.setMinimumHeight(RULER_H + ROW_H)

    # ------------------------------------------------------------------
    def set_project(self, project: Project) -> None:
        synced = [c for c in project.all_clips() if c.is_synced]
        if not synced:
            self.tl = None
        else:
            self.tl = build_timeline(project)
        self.ref_uid = next((c.uid for c in synced if c.status == "referência"), None)
        self._rows = []
        if self.tl:
            for tr in self.tl.tracks:
                for lane in range(tr.lanes):
                    self._rows.append((tr.name, lane, tr.is_video))
            # clipes que falharam: linha vermelha no fim, lado a lado (sem posição real)
            from ..timeline import TimelineItem
            cursor = 0.0
            gap = max(1.0, self.tl.duration * 0.01)
            failed = [c for c in project.all_clips()
                      if c.status in ("não sincronizado", "erro") and c.info]
            for c in failed:
                dur = max(c.duration, 1.0)
                self.tl.items.append(TimelineItem(c, c.path, c.info, cursor, dur, FAIL_ROW))
                cursor += dur + gap
            if failed:
                self._rows.append((FAIL_ROW, 0, False))
        self._relayout()

    def set_selected(self, uids: set[int]) -> None:
        self.selected = set(uids)
        self.update()

    def fit(self, width: int) -> None:
        if self.tl and self.tl.duration > 0:
            self.px_per_s = max(0.02, (width - LABEL_W - 30) / self.tl.duration)
            self._relayout()

    def zoom(self, factor: float) -> None:
        self.px_per_s = float(min(400.0, max(0.01, self.px_per_s * factor)))
        self._relayout()

    def _relayout(self) -> None:
        dur = self.tl.duration if self.tl else 0
        w = int(LABEL_W + dur * self.px_per_s + 40)
        h = RULER_H + max(1, len(self._rows)) * (ROW_H + ROW_GAP) + 10
        self.setMinimumSize(w, h)
        self.resize(max(w, self.parentWidget().width() if self.parentWidget() else w), h)
        self.update()

    # ------------------------------------------------------------------
    def _x(self, t: float) -> float:
        return LABEL_W + t * self.px_per_s

    def _tick_step(self) -> float:
        target = 90 / max(self.px_per_s, 1e-9)
        for s in (0.04, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
            if s >= target:
                return s
        return 7200

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), C_BG)
        vis = self.visibleRegion().boundingRect()
        left = vis.left()
        self._rects = []
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
        p.setFont(font)

        if not self.tl:
            p.setPen(C_DIM)
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Adicione arquivos e clique em Sincronizar para ver a timeline")
            return

        # linhas
        for k, (name, lane, is_video) in enumerate(self._rows):
            y = RULER_H + k * (ROW_H + ROW_GAP)
            bg = QColor("#2A0F0F") if name == FAIL_ROW else (C_ROW if k % 2 == 0 else C_ROW_ALT)
            p.fillRect(QRectF(0, y, self.width(), ROW_H), bg)

        # régua
        p.fillRect(QRectF(0, 0, self.width(), RULER_H), C_RULER)
        step = self._tick_step()
        t0 = max(0.0, (left - LABEL_W) / self.px_per_s)
        t1 = (vis.right() - LABEL_W) / self.px_per_s
        t = math.floor(t0 / step) * step
        p.setPen(QPen(C_DIM, 1))
        while t <= t1 + step:
            x = self._x(t)
            p.drawLine(QPointF(x, RULER_H - 8), QPointF(x, RULER_H))
            p.drawLine(QPointF(x, RULER_H), QPointF(x, self.height()))
            p.setPen(C_TEXT)
            p.drawText(QPointF(x + 3, RULER_H - 10), seconds_to_tc(t, self.tl.fps))
            p.setPen(QPen(QColor(255, 255, 255, 18), 1))
            t += step
        p.setPen(QPen(C_DIM, 1))

        # clipes
        row_of = {(n, l): k for k, (n, l, _) in enumerate(self._rows)}
        for it in self.tl.items:
            k = row_of.get((it.track, it.lane))
            if k is None:
                continue
            y = RULER_H + k * (ROW_H + ROW_GAP)
            x0, x1 = self._x(it.start), self._x(it.start + it.duration)
            r = QRectF(x0, y + 3, max(2.0, x1 - x0), ROW_H - 6)
            if r.right() < left or r.left() > vis.right():
                self._rects.append((r, it))
                continue
            failed = it.track == FAIL_ROW
            col = (C_FAIL if failed else C_REF if it.clip.uid == self.ref_uid
                   else (C_VIDEO if it.info.has_video else C_AUDIO))
            path = QPainterPath()
            path.addRoundedRect(r, 4, 4)
            p.fillPath(path, col)
            if it.clip.uid in self.selected:
                p.setPen(QPen(C_SEL, 2))
            else:
                p.setPen(QPen(col.darker(160), 1))
            p.drawPath(path)
            if r.width() > 30:
                p.setPen(QColor("#111111") if col.lightness() > 140 else Qt.white)
                tr = r.adjusted(6, 0, -4, 0)
                if tr.left() < left + LABEL_W:
                    tr.setLeft(min(tr.right(), left + LABEL_W + 4))
                p.drawText(tr, Qt.AlignVCenter | Qt.AlignLeft,
                           p.fontMetrics().elidedText(it.clip.name, Qt.ElideRight, int(tr.width())))
            self._rects.append((r, it))

        # rótulos das faixas (fixos à esquerda)
        p.fillRect(QRectF(left, 0, LABEL_W, self.height()), C_LABELS)
        p.fillRect(QRectF(left, 0, LABEL_W, RULER_H), C_RULER)
        p.setPen(C_DIM)
        p.drawText(QRectF(left + 8, 0, LABEL_W - 8, RULER_H), Qt.AlignVCenter, f"{float(self.tl.fps):.3f} fps".replace(".000", ""))
        for k, (name, lane, is_video) in enumerate(self._rows):
            y = RULER_H + k * (ROW_H + ROW_GAP)
            fail_row = name == FAIL_ROW
            p.fillRect(QRectF(left, y + 4, 4, ROW_H - 8),
                       C_FAIL if fail_row else (C_VIDEO if is_video else C_AUDIO))
            p.setPen(C_FAIL if fail_row else (C_TEXT if lane == 0 else C_DIM))
            label = name if lane == 0 else f"{name} ({lane + 1})"
            p.drawText(QRectF(left + 12, y, LABEL_W - 16, ROW_H), Qt.AlignVCenter,
                       p.fontMetrics().elidedText(label, Qt.ElideRight, LABEL_W - 16))
        p.setPen(QPen(YELLOW.darker(220), 1))
        p.drawLine(QPointF(left + LABEL_W, 0), QPointF(left + LABEL_W, self.height()))

    # ------------------------------------------------------------------
    def _item_at(self, pos):
        for r, it in self._rects:
            if r.contains(QPointF(pos)):
                return it
        return None

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        it = self._item_at(ev.position())
        if it is not None:
            self.clipClicked.emit(it.clip.uid)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        it = self._item_at(ev.position())
        if it is None:
            QToolTip.hideText()
            return
        c = it.clip
        lines = [f"<b>{c.name}</b>", f"Faixa: {it.track}",
                 f"Início: {seconds_to_tc(it.start, self.tl.fps)} ({it.start:.3f} s)",
                 f"Duração: {it.duration:.2f} s"]
        if it.track == FAIL_ROW:
            lines = [f"<b style='color:#FF4D4D'>✖ {c.name}</b>",
                     f"<span style='color:#FF4D4D'>{c.status.upper()}: {c.message}</span>",
                     "Posição desconhecida — verifique se o áudio é do mesmo evento."]
        elif c.status == "sincronizado":
            lines.append(f"Confiança: {c.confidence:.1f}")
        elif c.status == "referência":
            lines.append("Clipe de referência")
        if c.drift_reliable and abs(c.drift) > 1e-6:
            lines.append(f"Drift: {c.drift * 1e6:+.1f} ppm")
        if it.corrected:
            lines.append("Drift corrigido")
        QToolTip.showText(ev.globalPosition().toPoint(), "<br>".join(lines), self)

    def wheelEvent(self, ev) -> None:  # noqa: N802
        if ev.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            self.zoom(1.25 if ev.angleDelta().y() > 0 else 0.8)
            ev.accept()
        else:
            ev.ignore()


class TimelineView(QScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.canvas = TimelineCanvas()
        self.setWidget(self.canvas)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setStyleSheet("QScrollArea { background: #111111; border: none; }")
        self.horizontalScrollBar().valueChanged.connect(lambda _: self.canvas.update())

    def set_project(self, project: Project) -> None:
        self.canvas.set_project(project)
        self.canvas.fit(self.viewport().width())

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        if self.canvas.width() < self.viewport().width():
            self.canvas.resize(self.viewport().width(), self.canvas.height())
