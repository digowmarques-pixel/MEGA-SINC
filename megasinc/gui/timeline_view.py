"""Timeline visual (tela principal): faixas com cabeçalho, clipes com forma de
onda, progresso de preparação por clipe, arrastar clipes entre faixas."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QScrollArea, QToolTip, QWidget

from ..media import fps_label, seconds_to_tc
from ..prepare import WAVE_RATE
from ..project import Clip, Project, Status
from ..timeline import DEFAULT_FPS, build_timeline

HEAD_W = 250
RULER_H = 34
ROW_H = 74
SUB_H = 46          # sub-faixas (clipes sobrepostos do mesmo aparelho)
GAP_S = 0.02        # espaço entre clipes antes da sincronia (fração da duração)

# Paleta Mega Sinc
YELLOW = QColor("#FFC400")
YELLOW_LIGHT = QColor("#FFE27A")
GRAPHITE = QColor("#6B7079")
PENDING = QColor("#4A4A4A")
RED = QColor("#FF4D4D")
BG = QColor("#141414")
ROW_BG = QColor("#1b1b1b")
ROW_BG2 = QColor("#1f1f1f")
HEAD_BG = QColor("#181818")
RULER_BG = QColor("#0d0d0d")
TEXT = QColor("#ECECEC")
DIM = QColor("#8E8E8E")
FAIL_ROW = "__fail__"


@dataclass
class Row:
    track: str
    lane: int
    y: float
    h: float
    fail: bool = False


@dataclass
class Block:
    clip: Clip
    start: float
    dur: float
    row: int


def _icon_camera(p: QPainter, x: float, y: float, col: QColor) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    p.drawRoundedRect(QRectF(x, y + 6, 34, 22), 4, 4)
    p.drawRoundedRect(QRectF(x + 9, y + 2, 14, 7), 2, 2)
    p.setBrush(HEAD_BG)
    p.drawEllipse(QPointF(x + 17, y + 17), 7, 7)
    p.setBrush(col)
    p.drawEllipse(QPointF(x + 17, y + 17), 4, 4)


def _icon_mic(p: QPainter, x: float, y: float, col: QColor) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(col)
    p.drawRoundedRect(QRectF(x + 11, y + 1, 12, 20), 6, 6)
    p.setPen(QPen(col, 3, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(x + 6, y + 6, 22, 20), 200 * 16, 140 * 16)
    p.drawLine(QPointF(x + 17, y + 26), QPointF(x + 17, y + 31))
    p.drawLine(QPointF(x + 11, y + 31), QPointF(x + 23, y + 31))


class TimelineCanvas(QWidget):
    clipSelected = Signal(int)
    clipMoved = Signal(int, str)            # uid, faixa ("" = nova faixa)
    trackMenu = Signal(str, object)         # faixa, posição global
    clipMenu = Signal(int, object)          # uid, posição global
    trackRename = Signal(str)
    filesDropped = Signal(list, object)     # caminhos, faixa ou None
    deleteRequested = Signal()
    clipAdjusted = Signal(int, float, str)  # uid, novo início na timeline, faixa
                                            # ("" = mesma, "__new__" = nova faixa)
    nudgeRequested = Signal(int, int)       # uid, quadros (+/-)
    seekRequested = Signal(float)           # segundos na timeline
    mixChanged = Signal()                   # mudo/solo alterado

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.project: Optional[Project] = None
        self.fps = DEFAULT_FPS
        self.px_per_s = 2.0
        self.rows: list[Row] = []
        self.blocks: list[Block] = []
        self.synced_mode = False
        self.selected: Optional[int] = None
        self.ref_track: Optional[str] = None
        self.total = 0.0
        self._rects: list[tuple[QRectF, Block]] = []
        self._drag: Optional[tuple[int, QPointF]] = None
        self._press: Optional[tuple[Block, QPointF]] = None   # clipe e ponto do clique
        self._drag_active = False
        self.shift = 0.0         # deslocamento aplicado pela timeline
        self.playhead: Optional[float] = None
        self.muted: set[str] = set()
        self.soloed: set[str] = set()
        self._hdr_btns: list[tuple[QRectF, str, str]] = []
        self._drag_row: Optional[int] = None
        self._drop_row: Optional[int] = None
        self._auto_fit = True

    # ------------------------------------------------------------- modelo
    def set_project(self, project: Project, fps=None) -> None:
        self.project = project
        self.rebuild(fps)

    def rebuild(self, fps=None) -> None:
        pr = self.project
        self.rows, self.blocks = [], []
        if pr is None:
            self.update()
            return
        synced = [c for c in pr.all_clips() if c.is_synced]
        # estrela só na referência escolhida ou na que realmente foi usada
        root = next((c for c in synced if c.status == Status.REFERENCE), None)
        self.ref_track = pr.reference or (pr.track_of(root).name if root and pr.track_of(root)
                                          else None)
        self.synced_mode = bool(synced)
        y = RULER_H
        if self.synced_mode:
            tl = build_timeline(pr, fps)
            self.fps = tl.fps
            self.shift = tl.shift
            lanes = {t.name: t.lanes for t in tl.tracks}
            row_index = {}
            for t in pr.tracks:
                for lane in range(max(1, lanes.get(t.name, 1))):
                    h = ROW_H if lane == 0 else SUB_H
                    row_index[(t.name, lane)] = len(self.rows)
                    self.rows.append(Row(t.name, lane, y, h))
                    y += h + 2
            for it in tl.items:
                self.blocks.append(Block(it.clip, it.start, it.duration,
                                         row_index[(it.track, it.lane)]))
            failed = [c for c in pr.all_clips() if c.status in (Status.UNSYNCED, Status.ERROR)]
            if failed:
                self.rows.append(Row(FAIL_ROW, 0, y, ROW_H, fail=True))
                y += ROW_H + 2
                cur, gap = 0.0, max(1.0, tl.duration * GAP_S)
                for c in failed:
                    d = max(c.duration, 1.0)
                    self.blocks.append(Block(c, cur, d, len(self.rows) - 1))
                    cur += d + gap
        else:
            fpsc = [c.info.fps for c in pr.all_clips() if c.info and c.info.fps]
            self.fps = fps or (max(set(fpsc), key=fpsc.count) if fpsc else DEFAULT_FPS)
            longest = max((sum(max(c.duration, 1) for c in t.clips) for t in pr.tracks),
                          default=60.0)
            gap = max(0.5, longest * GAP_S)
            for t in pr.tracks:
                self.rows.append(Row(t.name, 0, y, ROW_H))
                cur = 0.0
                for c in t.clips:
                    d = max(c.duration, 1.0) if c.info else 30.0
                    self.blocks.append(Block(c, cur, d, len(self.rows) - 1))
                    cur += d + gap
                y += ROW_H + 2
        self.total = max((b.start + b.dur for b in self.blocks), default=0.0)
        self._content_h = y + ROW_H        # espaço extra = soltar para "nova faixa"
        self._relayout()

    # ---------------------------------------------------------------- zoom
    def fit(self, width: int) -> None:
        if self.total > 0:
            self.px_per_s = max(0.005, (width - HEAD_W - 40) / self.total)
        self._relayout()

    def zoom(self, factor: float) -> None:
        self._auto_fit = False
        self.px_per_s = float(min(500.0, max(0.005, self.px_per_s * factor)))
        self._relayout()

    def _relayout(self) -> None:
        w = int(HEAD_W + self.total * self.px_per_s + 60)
        h = int(getattr(self, "_content_h", RULER_H + ROW_H))
        par = self.parentWidget()
        if par is not None:
            w = max(w, par.width())
            h = max(h, par.height())
        self.setMinimumSize(w, h)
        self.resize(w, h)
        self.update()

    def x_of(self, t: float) -> float:
        return HEAD_W + t * self.px_per_s

    # --------------------------------------------------------------- pintura
    def _tick(self) -> float:
        target = 110 / max(self.px_per_s, 1e-9)
        for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
            if s >= target:
                return s
        return 7200

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        vis = self.visibleRegion().boundingRect()
        left, right = vis.left(), vis.right()
        p.fillRect(self.rect(), BG)
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
        p.setFont(f)
        fm = p.fontMetrics()

        if not self.rows:
            p.setPen(DIM)
            big = QFont(self.font())
            big.setPointSizeF(big.pointSizeF() + 5)
            p.setFont(big)
            p.drawText(QRectF(left, 0, vis.width(), vis.height()), Qt.AlignCenter,
                       "Arraste aqui as pastas ou arquivos\ndas câmeras e gravadores")
            p.setPen(QPen(QColor("#333"), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(left + 40, 40, vis.width() - 80, vis.height() - 80), 16, 16)
            return

        # fundo das linhas
        for k, r in enumerate(self.rows):
            col = QColor("#2A0F0F") if r.fail else (ROW_BG if k % 2 == 0 else ROW_BG2)
            if self._drop_row == k:
                col = QColor("#3a3220")
            p.fillRect(QRectF(0, r.y, self.width(), r.h), col)
        if self._drop_row == len(self.rows):
            y = self.rows[-1].y + self.rows[-1].h + 2
            p.fillRect(QRectF(left, y, vis.width(), ROW_H), QColor("#3a3220"))
            p.setPen(YELLOW)
            p.drawText(QRectF(left + HEAD_W, y, vis.width() - HEAD_W, ROW_H), Qt.AlignCenter,
                       "Soltar aqui para criar uma nova faixa")

        # grade e régua
        step = self._tick()
        t0 = max(0.0, (left - HEAD_W) / self.px_per_s)
        t1 = (right - HEAD_W) / self.px_per_s
        t = math.floor(t0 / step) * step
        p.fillRect(QRectF(left, 0, vis.width(), RULER_H), RULER_BG)
        minor = step / 5
        while t <= t1 + step:
            x = self.x_of(t)
            p.setPen(QPen(QColor(255, 255, 255, 14), 1))
            p.drawLine(QPointF(x, RULER_H), QPointF(x, self.height()))
            p.setPen(QPen(DIM, 1))
            p.drawLine(QPointF(x, RULER_H - 12), QPointF(x, RULER_H))
            for m in range(1, 5):
                xm = self.x_of(t + m * minor)
                p.drawLine(QPointF(xm, RULER_H - 5), QPointF(xm, RULER_H))
            p.setPen(TEXT)
            p.drawText(QPointF(x + 4, RULER_H - 15), seconds_to_tc(t, self.fps))
            t += step

        # clipes
        self._rects = []
        for b in self.blocks:
            r = self.rows[b.row]
            x0, x1 = self.x_of(b.start), self.x_of(b.start + b.dur)
            rect = QRectF(x0, r.y + 4, max(3.0, x1 - x0 - 1), r.h - 8)
            self._rects.append((rect, b))
            if rect.right() < left + HEAD_W or rect.left() > right:
                continue
            self._paint_clip(p, rect, b, r, left, right, fm)

        # cursor de reprodução
        if self.playhead is not None and self.synced_mode:
            xp = self.x_of(self.playhead)
            p.setPen(QPen(QColor("#FFFFFF"), 2))
            p.drawLine(QPointF(xp, RULER_H), QPointF(xp, self.height()))
            p.setPen(Qt.NoPen)
            p.setBrush(YELLOW)
            p.drawPolygon(QPolygonF([QPointF(xp - 7, RULER_H - 12), QPointF(xp + 7, RULER_H - 12),
                                     QPointF(xp, RULER_H)]))

        # cabeçalhos (fixos à esquerda)
        self._hdr_btns = []
        p.fillRect(QRectF(left, 0, HEAD_W, self.height()), HEAD_BG)
        p.fillRect(QRectF(left, 0, HEAD_W, RULER_H), RULER_BG)
        p.setPen(YELLOW)
        p.drawText(QRectF(left + 14, 0, HEAD_W - 20, RULER_H), Qt.AlignVCenter,
                   "SINCRONIZADO" if self.synced_mode else "MÍDIA")
        p.setPen(DIM)
        p.drawText(QRectF(left, 0, HEAD_W - 12, RULER_H), Qt.AlignVCenter | Qt.AlignRight,
                   f"{fps_label(self.fps)} fps")
        for k, r in enumerate(self.rows):
            self._paint_header(p, r, left, fm)
        p.setPen(QPen(QColor("#000"), 2))
        p.drawLine(QPointF(left + HEAD_W, 0), QPointF(left + HEAD_W, self.height()))

        # fantasma do arraste
        if self._drag_active and self._press is not None and self._drag is not None:
            blk, p0 = self._press
            pos = self._drag[1]
            k = self._drag_row
            if self.synced_mode and k is not None:
                t_new = self._dragged_start(blk, p0, pos)
                if k < len(self.rows) and not self.rows[k].fail:
                    y, h = self.rows[k].y, self.rows[k].h
                elif k == len(self.rows):
                    y, h = self.rows[-1].y + self.rows[-1].h + 2, ROW_H
                else:
                    y, h = self.rows[blk.row].y, self.rows[blk.row].h
                x0 = self.x_of(t_new)
                ghost = QRectF(x0, y + 4, max(3.0, blk.dur * self.px_per_s - 1), h - 8)
                p.setPen(QPen(QColor("#FFFFFF"), 2, Qt.DashLine))
                p.setBrush(QColor(255, 196, 0, 70))
                p.drawRoundedRect(ghost, 3, 3)
                p.setPen(QPen(YELLOW, 1))
                p.drawLine(QPointF(x0, RULER_H), QPointF(x0, self.height()))
                lab = (f"{seconds_to_tc(max(0.0, t_new), self.fps)}   "
                       f"({t_new - blk.start:+.3f} s)")
                p.fillRect(QRectF(x0 + 4, RULER_H + 2, fm.horizontalAdvance(lab) + 10, 18),
                           QColor("#000000"))
                p.setPen(YELLOW)
                p.drawText(QPointF(x0 + 9, RULER_H + 15), lab)
            elif k is not None:
                p.setPen(QPen(YELLOW, 2, Qt.DashLine))
                p.setBrush(QColor(255, 196, 0, 40))
                p.drawRoundedRect(QRectF(pos.x() - 60, pos.y() - 18, 180, 36), 6, 6)
                p.setPen(TEXT)
                p.drawText(QRectF(pos.x() - 54, pos.y() - 18, 170, 36), Qt.AlignVCenter,
                           fm.elidedText(blk.clip.name, Qt.ElideRight, 165))

    def active_tracks(self) -> Optional[set[str]]:
        """Faixas audíveis no player (None = todas)."""
        if self.soloed:
            return set(self.soloed)
        if self.muted:
            return {r.track for r in self.rows if not r.fail} - self.muted
        return None

    def ensure_visible(self, t: float) -> None:
        sa = self.parentWidget().parentWidget() if self.parentWidget() else None
        if sa is None or not hasattr(sa, "horizontalScrollBar"):
            return
        vis = self.visibleRegion().boundingRect()
        x = self.x_of(t)
        if x > vis.right() - 60 or x < vis.left() + HEAD_W:
            sa.horizontalScrollBar().setValue(int(max(0, x - HEAD_W - 80)))

    def _dragged_start(self, blk: Block, p0: QPointF, pos: QPointF) -> float:
        t = blk.start + (pos.x() - p0.x()) / self.px_per_s
        f = float(self.fps)
        return round(t * f) / f          # encaixa no quadro

    def _clip_color(self, b: Block, row: Row) -> QColor:
        c = b.clip
        if row.fail or c.status in (Status.UNSYNCED, Status.ERROR):
            return RED
        if not self.synced_mode:
            return PENDING if c.prep < 1.0 else QColor("#5a5a5a")
        if c.status == Status.REFERENCE:
            return YELLOW_LIGHT
        return YELLOW if c.has_video else GRAPHITE

    def _paint_clip(self, p: QPainter, rect: QRectF, b: Block, row: Row, left, right, fm):
        c = b.clip
        col = self._clip_color(b, row)
        path = QPainterPath()
        path.addRoundedRect(rect, 3, 3)
        p.fillPath(path, col)
        dark_text = 0.299 * col.red() + 0.587 * col.green() + 0.114 * col.blue() > 150
        wave_col = QColor(0, 0, 0, 120) if dark_text else QColor(255, 255, 255, 90)

        # forma de onda (só a parte visível)
        wave = c.wave
        if wave is not None and len(wave) > 1 and rect.width() > 6:
            vx0 = max(rect.left(), left + HEAD_W)
            vx1 = min(rect.right(), right)
            if vx1 > vx0:
                cols = int(vx1 - vx0)
                ts = (np.arange(cols + 1) + (vx0 - rect.left())) / rect.width() * b.dur
                w = np.asarray(wave, dtype=np.float32)
                # pico de cada coluna de pixel = máximo de wave[edges[i]:edges[i+1]]
                edges = np.clip((ts * WAVE_RATE).astype(np.int64), 0, len(w) - 1)
                peaks = np.maximum.reduceat(w, edges)[:cols]
                base = rect.bottom() - 2
                amp = (rect.height() - 22) * np.sqrt(peaks)
                pts = [QPointF(vx0, base)]
                pts += [QPointF(vx0 + i, base - a) for i, a in enumerate(amp)]
                pts.append(QPointF(vx0 + cols, base))
                p.setPen(Qt.NoPen)
                p.setBrush(wave_col)
                p.drawPolygon(QPolygonF(pts))

        # texto: nome | duração | estado
        if rect.width() > 40:
            state = {Status.SYNCED: "sincronizado", Status.REFERENCE: "★ referência",
                     Status.UNSYNCED: "✖ não sincronizado", Status.ERROR: "✖ erro"}.get(c.status)
            if state is None:
                state = "pronto" if c.prep >= 1.0 else ("preparando" if c.prep > 0 else "na fila")
            label = f"{c.name}  |  {seconds_to_tc(c.duration, self.fps)}  |  {state}"
            tr = QRectF(max(rect.left(), left + HEAD_W) + 6, rect.top() + 2,
                        rect.right() - max(rect.left(), left + HEAD_W) - 10, 16)
            p.setPen(QColor("#111") if dark_text else TEXT)
            p.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(label, Qt.ElideRight, int(max(10, tr.width()))))

        # progresso de preparação
        if 0.0 <= c.prep < 1.0 and not self.synced_mode:
            p.fillRect(QRectF(rect.left(), rect.bottom() - 4, rect.width(), 4), QColor("#222"))
            p.fillRect(QRectF(rect.left(), rect.bottom() - 4, rect.width() * c.prep, 4), YELLOW)

        if self.selected == c.uid:
            p.setPen(QPen(QColor("#FFFFFF"), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)

    def _paint_header(self, p: QPainter, r: Row, left: float, fm) -> None:
        x = left
        if r.fail:
            p.fillRect(QRectF(x, r.y, 5, r.h), RED)
            p.setPen(RED)
            bf = QFont(self.font())
            bf.setBold(True)
            p.setFont(bf)
            p.drawText(QRectF(x + 18, r.y + 12, HEAD_W - 24, 22), Qt.AlignVCenter,
                       "✖ Não sincronizados")
            p.setFont(self.font())
            n = sum(1 for b in self.blocks if self.rows[b.row] is r)
            p.drawText(QRectF(x + 18, r.y + 36, HEAD_W - 24, 20), Qt.AlignVCenter,
                       f"{n} clipe(s) sem correspondência")
            return
        t = self.project.track(r.track) if self.project else None
        if t is None:
            return
        if r.lane > 0:
            p.setPen(DIM)
            p.drawText(QRectF(x + 62, r.y, HEAD_W - 70, r.h), Qt.AlignVCenter,
                       f"{t.name} — sobreposição {r.lane + 1}")
            return
        is_ref = r.track == self.ref_track
        bad = any(c.status in (Status.UNSYNCED, Status.ERROR) for c in t.clips)
        accent = RED if bad else (YELLOW if t.has_video or is_ref else GRAPHITE.lighter(140))
        p.fillRect(QRectF(x, r.y, 5, r.h), accent)
        icon_col = YELLOW if is_ref else QColor("#BDBDBD")
        if t.has_video:
            _icon_camera(p, x + 16, r.y + 18, icon_col)
        else:
            _icon_mic(p, x + 16, r.y + 18, icon_col)
        bf = QFont(self.font())
        bf.setBold(True)
        bf.setPointSizeF(bf.pointSizeF() + 1)
        p.setFont(bf)
        p.setPen(TEXT)
        name = t.name + ("  ★" if is_ref else "")
        name_w = HEAD_W - (130 if self.synced_mode else 70)
        p.drawText(QRectF(x + 60, r.y + 10, name_w, 24), Qt.AlignVCenter,
                   p.fontMetrics().elidedText(name, Qt.ElideRight, name_w))
        p.setFont(self.font())
        info = [f"{len(t.clips)} clipe" + ("s" if len(t.clips) != 1 else "")]
        fpss = {c.info.fps for c in t.clips if c.info and c.info.fps}
        if len(fpss) == 1:
            info.append(f"{fps_label(next(iter(fpss)))} fps")
        chs = {c.info.audio_channels for c in t.clips if c.info and c.info.has_audio}
        if t.audio_channel:
            info.append(f"análise: canal {t.audio_channel}")
        elif len(chs) == 1:
            n = next(iter(chs))
            info.append(f"{n} canal" + ("is" if n != 1 else ""))
        p.setPen(DIM)
        p.drawText(QRectF(x + 60, r.y + 34, HEAD_W - 66, 20), Qt.AlignVCenter,
                   p.fontMetrics().elidedText("  |  ".join(info), Qt.ElideRight, HEAD_W - 70))
        if self.synced_mode:
            for i, (kind, on_col) in enumerate((("M", RED), ("S", YELLOW))):
                br = QRectF(x + HEAD_W - 60 + i * 26, r.y + 13, 22, 18)
                on = r.track in (self.muted if kind == "M" else self.soloed)
                p.setPen(QPen(on_col if on else QColor("#555"), 1))
                p.setBrush(on_col if on else QColor("#222"))
                p.drawRoundedRect(br, 3, 3)
                p.setPen(QColor("#111") if on else DIM)
                p.drawText(br, Qt.AlignCenter, kind)
                self._hdr_btns.append((br, r.track, kind))
        if is_ref:
            p.setPen(YELLOW)
            p.drawText(QRectF(x + 60, r.y + 52, HEAD_W - 66, 16), Qt.AlignVCenter, "referência")
        elif bad:
            p.setPen(RED)
            p.drawText(QRectF(x + 60, r.y + 52, HEAD_W - 66, 16), Qt.AlignVCenter,
                       "tem clipe não sincronizado")

    # ------------------------------------------------------------- interação
    def _row_at(self, y: float) -> Optional[int]:
        for k, r in enumerate(self.rows):
            if r.y <= y < r.y + r.h + 2:
                return k
        if self.rows and y >= self.rows[-1].y + self.rows[-1].h:
            return len(self.rows)        # área vazia abaixo = nova faixa
        return None

    def _block_at(self, pos: QPointF) -> Optional[Block]:
        for rect, b in reversed(self._rects):
            if rect.contains(pos):
                return b
        return None

    def _in_header(self, pos: QPointF) -> bool:
        return pos.x() < self.visibleRegion().boundingRect().left() + HEAD_W

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        pos = ev.position()
        self.setFocus()
        if self._in_header(pos):
            for br, track, kind in self._hdr_btns:
                if br.contains(pos) and ev.button() == Qt.LeftButton:
                    group = self.muted if kind == "M" else self.soloed
                    group.symmetric_difference_update({track})
                    self.mixChanged.emit()
                    self.update()
                    return
            k = self._row_at(pos.y())
            if k is not None and k < len(self.rows) and not self.rows[k].fail and \
                    ev.button() == Qt.RightButton:
                self.trackMenu.emit(self.rows[k].track, ev.globalPosition().toPoint())
            return
        if pos.y() < RULER_H and self.synced_mode:
            self.seekRequested.emit(max(0.0, (pos.x() - HEAD_W) / self.px_per_s))
            return
        b = self._block_at(pos)
        self.selected = b.clip.uid if b else None
        if b:
            self.clipSelected.emit(b.clip.uid)
            if ev.button() == Qt.RightButton:
                self.clipMenu.emit(b.clip.uid, ev.globalPosition().toPoint())
            elif ev.button() == Qt.LeftButton:
                self._press = (b, pos)
                self._drag = (b.clip.uid, pos)
                self._drag_active = False
        self.update()

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        pos = ev.position()
        if self._in_header(pos):
            k = self._row_at(pos.y())
            if k is not None and k < len(self.rows) and not self.rows[k].fail:
                self.trackRename.emit(self.rows[k].track)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        pos = ev.position()
        if self._press and ev.buttons() & Qt.LeftButton:
            blk, p0 = self._press
            if self._drag_active or (pos - p0).manhattanLength() > 6:
                self._drag_active = True
                self._drag = (blk.clip.uid, pos)
                self._drag_row = self._row_at(pos.y())
                self._drop_row = None if self.synced_mode else self._drag_row
                self.update()
            return
        b = self._block_at(pos)
        if b is None or self._in_header(pos):
            QToolTip.hideText()
            return
        c = b.clip
        lines = [f"<b>{c.name}</b>", c.path]
        if c.info:
            if c.info.has_video:
                lines.append(f"Vídeo {c.info.width}×{c.info.height} · {fps_label(c.info.fps)} fps")
            if c.info.has_audio:
                lines.append(f"Áudio {c.info.audio_rate / 1000:g} kHz · "
                             f"{c.info.audio_channels} canal(is)")
        if self.synced_mode and c.is_synced:
            lines.append(f"Início na timeline: {seconds_to_tc(b.start, self.fps)}")
            if c.status == Status.SYNCED:
                lines.append(f"Confiança: {c.confidence:.0f}")
            if c.drift_reliable and abs(c.drift) > 5e-7:
                lines.append(f"Drift: {c.drift * 1e6:+.1f} ppm")
        if c.status in (Status.UNSYNCED, Status.ERROR):
            lines.append(f"<span style='color:#FF4D4D'><b>✖ {c.status.upper()}</b>"
                         f"{': ' + c.message if c.message else ''}</span>")
        QToolTip.showText(ev.globalPosition().toPoint(), "<br>".join(lines), self)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if self._press and self._drag_active and self._drag is not None:
            blk, p0 = self._press
            pos = self._drag[1]
            k = self._drag_row
            cur = self.project.track_of(blk.clip) if self.project else None
            if self.synced_mode:
                t_new = self._dragged_start(blk, p0, pos)
                target = ""
                if k == len(self.rows):
                    target = "__new__"
                elif k is not None and 0 <= k < len(self.rows) and not self.rows[k].fail:
                    if cur is None or self.rows[k].track != cur.name:
                        target = self.rows[k].track
                failed_row = self.rows[blk.row].fail
                if abs(t_new - blk.start) > 1e-9 or target or failed_row:
                    self.clipAdjusted.emit(blk.clip.uid, t_new, target)
            elif k is not None:
                if k == len(self.rows):
                    self.clipMoved.emit(blk.clip.uid, "")
                elif 0 <= k < len(self.rows) and not self.rows[k].fail:
                    if cur is not None and cur.name != self.rows[k].track:
                        self.clipMoved.emit(blk.clip.uid, self.rows[k].track)
        self._press = None
        self._drag = None
        self._drag_active = False
        self._drag_row = None
        self._drop_row = None
        self.update()

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.selected is not None:
            self.deleteRequested.emit()
        elif ev.key() in (Qt.Key_Left, Qt.Key_Right) and self.selected is not None \
                and self.synced_mode:
            step = 10 if ev.modifiers() & Qt.ShiftModifier else 1
            self.nudgeRequested.emit(self.selected,
                                     step if ev.key() == Qt.Key_Right else -step)
        else:
            super().keyPressEvent(ev)

    def wheelEvent(self, ev) -> None:  # noqa: N802
        if ev.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            self.zoom(1.25 if ev.angleDelta().y() > 0 else 0.8)
            ev.accept()
        else:
            ev.ignore()

    # arrastar arquivos do Finder/Explorer
    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            k = self._row_at(ev.position().y())
            self._drop_row = k if (k is not None and (k == len(self.rows) or
                                                      not self.rows[k].fail)) else None
            self.update()
            ev.acceptProposedAction()

    def dragLeaveEvent(self, _ev) -> None:  # noqa: N802
        self._drop_row = None
        self.update()

    def dropEvent(self, ev) -> None:  # noqa: N802
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        k = self._row_at(ev.position().y())
        target = None
        if k is not None and k < len(self.rows) and not self.rows[k].fail and self.rows:
            target = self.rows[k].track
        self._drop_row = None
        self.update()
        if paths:
            self.filesDropped.emit(paths, target)


class TimelineView(QScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.canvas = TimelineCanvas()
        self.setWidget(self.canvas)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setStyleSheet("QScrollArea { background: #141414; border: none; }")
        self.horizontalScrollBar().valueChanged.connect(lambda _: self.canvas.update())
        self.verticalScrollBar().valueChanged.connect(lambda _: self.canvas.update())

    def set_project(self, project: Project, fps=None, fit: bool = True) -> None:
        self.canvas.set_project(project, fps)
        if fit or self.canvas._auto_fit:
            self.canvas._auto_fit = True
            self.canvas.fit(self.viewport().width())

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        if self.canvas._auto_fit:
            self.canvas.fit(self.viewport().width())
        else:
            self.canvas._relayout()
