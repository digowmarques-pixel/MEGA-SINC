"""Janela principal do Mega Sinc."""
from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QFont, QKeySequence,
                           QPainter, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox,
                               QComboBox, QDialog, QDialogButtonBox, QDockWidget,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
                               QSizePolicy, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..engine import SyncEngine, SyncOptions
from ..export import (export_csv, export_drift_corrected, export_fcpxml,
                      export_fcpxml_multicam, export_json,
                      export_replaced_audio, export_xmeml)
from ..export.media_export import drift_candidates
from ..media import (MEDIA_EXTS, channel_count, default_cache_dir, open_pcm, describe_tools, find_tool, fps_label,
                     parse_fps_string, seconds_to_tc)
from ..player import Player, Source
from ..prepare import cache_paths, prepare_clip
from ..project import GROUP_MODES, Project, Status
from ..timeline import build_timeline, guess_fps
from .timeline_view import TimelineView
from .workers import TaskThread

YELLOW = "#FFC400"
RED = "#FF4D4D"
RED_BG = "#3A1212"
GREEN = "#7BD88F"
FAILED = (Status.UNSYNCED, Status.ERROR)
STATUS_COLORS = {Status.REFERENCE: YELLOW, Status.SYNCED: GREEN, Status.UNSYNCED: RED,
                 Status.ERROR: RED, Status.PENDING: "#8E8E8E"}
COLS = ["Faixa / Arquivo", "Tipo", "Duração", "Posição", "Confiança", "Drift", "Status"]
FPS_CHOICES = ["Automático", "23.976", "24", "25", "29.97", "30", "48", "50", "59.94", "60"]


def fmt_dur(s: float) -> str:
    if not s:
        return "—"
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def reveal_in_os(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))


# --------------------------------------------------------------------------
# Barra de status larga (progresso / resultado)
# --------------------------------------------------------------------------
class StatusStrip(QWidget):
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(36)
        self.text = ""
        self.frac: Optional[float] = None
        self.mode = "idle"            # idle | progress | ok | fail
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.addStretch()
        self.cancel_btn = QPushButton("Cancelar")
        self.cancel_btn.setObjectName("cancelButton")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.cancelRequested)
        lay.addWidget(self.cancel_btn)

    def idle(self, text: str) -> None:
        self.mode, self.text, self.frac = "idle", text, None
        self.cancel_btn.setVisible(False)
        self.update()

    def progress(self, frac: float, text: str) -> None:
        self.mode, self.text, self.frac = "progress", text, frac
        self.cancel_btn.setVisible(True)
        self.update()

    def result(self, text: str, ok: bool) -> None:
        self.mode, self.text, self.frac = ("ok" if ok else "fail"), text, None
        self.cancel_btn.setVisible(False)
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        r = self.rect()
        fg = QColor("#ECECEC")
        if self.mode == "progress":
            p.fillRect(r, QColor("#2a2a2a"))
            p.fillRect(0, 0, int(r.width() * (self.frac or 0)), r.height(), QColor(YELLOW))
            text = f"{self.text} — {int((self.frac or 0) * 100)}%"
        elif self.mode == "ok":
            p.fillRect(r, QColor(YELLOW))
            fg, text = QColor("#111111"), self.text
        elif self.mode == "fail":
            p.fillRect(r, QColor("#C62828"))
            fg, text = QColor("#FFFFFF"), self.text
        else:
            p.fillRect(r, QColor("#101010"))
            fg, text = QColor("#BDBDBD"), self.text
        f = QFont(self.font())
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() + 1)
        p.setFont(f)
        if self.mode == "progress":
            # texto legível sobre as duas cores
            p.setPen(QColor("#111111"))
            p.setClipRect(0, 0, int(r.width() * (self.frac or 0)), r.height())
            p.drawText(r, Qt.AlignCenter, text)
            p.setClipRect(int(r.width() * (self.frac or 0)), 0, r.width(), r.height())
            p.setPen(QColor("#ECECEC"))
            p.drawText(r, Qt.AlignCenter, text)
        else:
            p.setPen(fg)
            p.drawText(r, Qt.AlignCenter, text)


# --------------------------------------------------------------------------
# Lista detalhada (painel opcional)
# --------------------------------------------------------------------------
class ClipTree(QTreeWidget):
    clipsMoved = Signal(list, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setColumnCount(len(COLS))
        self.setHeaderLabels(COLS)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(COLS)):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        h.setStretchLastSection(False)

    def dropEvent(self, ev) -> None:  # noqa: N802
        item = self.itemAt(ev.position().toPoint())
        ev.setDropAction(Qt.IgnoreAction)
        ev.accept()
        if item is None:
            return
        if item.parent() is not None:
            item = item.parent()
        kind, target = item.data(0, Qt.UserRole)
        uids = [it.data(0, Qt.UserRole)[1] for it in self.selectedItems()
                if it.data(0, Qt.UserRole)[0] == "clip"]
        if kind == "track" and uids:
            self.clipsMoved.emit(uids, target)


# --------------------------------------------------------------------------
# Diálogos
# --------------------------------------------------------------------------
class OptionsDialog(QDialog):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win)
        self.setWindowTitle("Opções de sincronização")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        o = win.opts
        self.group = QComboBox()
        for k, v in GROUP_MODES.items():
            self.group.addItem(v, k)
        self.group.setCurrentIndex(max(0, self.group.findData(o["group"])))
        form.addRow("Agrupar novos arquivos:", self.group)
        self.ref = QComboBox()
        self.ref.addItem("Automática (gravador mais longo)", None)
        for t in win.project.tracks:
            self.ref.addItem(t.name, t.name)
        self.ref.setCurrentIndex(max(0, self.ref.findData(win.project.reference))
                                 if win.project.reference else 0)
        form.addRow("Faixa de referência:", self.ref)
        self.method = QComboBox()
        self.method.addItem("Áudio", "audio")
        self.method.addItem("Áudio + timecode (quando o áudio falhar)", "hybrid")
        self.method.addItem("Somente timecode", "timecode")
        self.method.setCurrentIndex(max(0, self.method.findData(o["method"])))
        self.method.setToolTip("Timecode só funciona se as câmeras e o gravador estavam\n"
                               "com o timecode sincronizado (jam sync) na gravação.")
        form.addRow("Sincronizar por:", self.method)
        self.clock = QComboBox()
        self.clock.addItem("Câmeras (corrige o drift do áudio)", "video")
        self.clock.addItem("Faixa de referência", "reference")
        self.clock.setCurrentIndex(max(0, self.clock.findData(o["clock"])))
        form.addRow("Relógio da timeline:", self.clock)
        self.fps = QComboBox()
        self.fps.setEditable(True)
        self.fps.addItems(FPS_CHOICES)
        self.fps.setCurrentText(o["fps"])
        form.addRow("FPS da timeline:", self.fps)
        self.score = QDoubleSpinBox()
        self.score.setRange(4.0, 40.0)
        self.score.setSingleStep(0.5)
        self.score.setValue(o["min_score"])
        self.score.setToolTip("Menor = encontra mais (risco de erro); maior = mais rigoroso.")
        form.addRow("Confiança mínima:", self.score)
        self.fcpx = QComboBox()
        self.fcpx.addItems(["1.10", "1.11", "1.9", "1.8"])
        self.fcpx.setCurrentText(o["fcpxml_version"])
        form.addRow("Versão do FCPXML:", self.fcpx)
        lay.addLayout(form)
        self.same = QCheckBox("Comparar clipes da mesma faixa (câmera com arquivos sobrepostos)")
        self.same.setChecked(o["same_track"])
        lay.addWidget(self.same)
        self.clockbox = QCheckBox("Usar o horário de gravação das câmeras (recomendado)")
        self.clockbox.setToolTip(
            "Descarta correspondências falsas, procura clipes difíceis perto da posição\n"
            "prevista e posiciona clipes/grupos que não têm áudio em comum.")
        self.clockbox.setChecked(o["use_clock"])
        lay.addWidget(self.clockbox)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def apply(self, win: "MainWindow") -> None:
        win.opts.update(group=self.group.currentData(), clock=self.clock.currentData(),
                        method=self.method.currentData(),
                        fps=self.fps.currentText(), min_score=self.score.value(),
                        fcpxml_version=self.fcpx.currentText(),
                        same_track=self.same.isChecked(),
                        use_clock=self.clockbox.isChecked())
        win.project.reference = self.ref.currentData()


EXPORT_TARGETS = [
    ("premiere", "Adobe Premiere Pro",
     "XML → Arquivo › Importar. Multicâmera: arraste a sequência importada para uma nova\n"
     "sequência, clique com o botão direito no clipe › Multicâmera › Ativar."),
    ("multicam", "Final Cut Pro / DaVinci Resolve — clipe multicâmera",
     "Cada câmera vira um ângulo, com o áudio do gravador. Pronto para cortar."),
    ("fcpx", "Final Cut Pro — timeline", "FCPXML → Arquivo › Importar › XML"),
    ("resolve", "DaVinci Resolve — timeline", "FCPXML → File › Import › Timeline"),
    ("media", "Vídeos com o áudio do gravador", "Novos .mov já sincronizados (sem recodificar)"),
    ("csv", "Relatório (planilha CSV)", "Lista com posições, confiança e drift"),
]


class ExportDialog(QDialog):
    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win)
        self.setWindowTitle("Exportar timeline")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        title = QLabel(f"<span style='font-size:17px; font-weight:700; color:{YELLOW}'>"
                       "Para onde vai a timeline?</span>")
        lay.addWidget(title)
        self.group = QButtonGroup(self)
        last = win.settings.value("export_target", "premiere")
        for key, name, desc in EXPORT_TARGETS:
            rb = QRadioButton(f"{name}\n{desc}")
            rb.setObjectName("exportOption")
            rb.setProperty("key", key)
            rb.setChecked(key == last)
            self.group.addButton(rb)
            lay.addWidget(rb)
        self.drift = QCheckBox("Corrigir drift do áudio (recomendado em gravações > 10 min)")
        self.drift.setChecked(win.settings.value("drift_fix", "false") == "true")
        self.unsynced = QCheckBox("Incluir os não sincronizados no fim da timeline")
        self.mute = QCheckBox("Silenciar o áudio das câmeras (usar só o do gravador)")
        self.mute.setChecked(win.settings.value("mute_camera_audio", "false") == "true")
        lay.addSpacing(6)
        lay.addWidget(self.drift)
        lay.addWidget(self.mute)
        lay.addWidget(self.unsynced)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        ok = bb.addButton("Exportar…", QDialogButtonBox.AcceptRole)
        ok.setObjectName("primaryButton")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def target(self) -> str:
        b = self.group.checkedButton()
        return b.property("key") if b else "premiere"


class ReplaceAudioDialog(QDialog):
    def __init__(self, tracks: list[str], default: Optional[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Vídeos com áudio do gravador")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Cria cópias dos vídeos (sem recodificar a imagem) com o áudio do gravador\n"
            "sincronizado na trilha 1. Saem em .mov com áudio PCM 24-bit."))
        form = QFormLayout()
        self.track = QComboBox()
        self.track.addItems(tracks)
        if default in tracks:
            self.track.setCurrentText(default)
        form.addRow("Áudio de:", self.track)
        self.keep = QCheckBox("Manter o áudio original da câmera nas trilhas seguintes")
        self.keep.setChecked(True)
        form.addRow("", self.keep)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


# --------------------------------------------------------------------------
# Janela principal
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1440, 860)
        self.project = Project()
        self.settings = QSettings("MegaSinc", "MegaSinc")
        self.task: Optional[TaskThread] = None
        self.project_path: Optional[str] = None
        self.cache_dir = default_cache_dir()
        self._probed = 0
        self._undo: list[dict] = []      # ajustes manuais (para desfazer)
        self.opts = {}
        self._load_settings()
        self._build_ui()
        self.refresh()
        if not find_tool("ffmpeg"):
            QMessageBox.warning(self, APP_NAME, "FFmpeg não foi encontrado.\n\n" + describe_tools())

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        # ---- barra superior
        top = QFrame()
        top.setObjectName("topBar")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(14, 8, 14, 8)
        from .app import draw_logo
        logo = QLabel()
        logo.setPixmap(draw_logo(36))
        tl.addWidget(logo)
        brand = QLabel(f"<span style='color:{YELLOW}; font-size:19px; font-weight:800;"
                       f" letter-spacing:2px'>MEGA</span><span style='color:#ECECEC;"
                       f" font-size:19px; font-weight:300; letter-spacing:2px'> SINC</span>")
        tl.addWidget(brand)
        tl.addStretch(1)
        self.btn_add = self._step_button("＋  Adicionar mídia", self.on_add_menu)
        self.btn_sync = self._step_button("▶  Sincronizar", self.on_sync)
        self.btn_export = self._step_button("⇪  Exportar timeline", self.on_export)
        for b in (self.btn_add, self.btn_sync, self.btn_export):
            tl.addWidget(b)
        tl.addStretch(1)
        self.btn_opts = QPushButton("⚙  Opções")
        self.btn_opts.setObjectName("flatButton")
        self.btn_opts.clicked.connect(self.on_options)
        tl.addWidget(self.btn_opts)
        self.btn_details = QPushButton("☰  Detalhes")
        self.btn_details.setObjectName("flatButton")
        self.btn_details.setCheckable(True)
        tl.addWidget(self.btn_details)

        # ---- zoom
        zoom = QFrame()
        zoom.setObjectName("zoomBar")
        zl = QHBoxLayout(zoom)
        zl.setContentsMargins(12, 2, 12, 2)
        self.btn_play = QPushButton("▶  Ouvir")
        self.btn_play.setObjectName("playButton")
        self.btn_play.setToolTip("Toca a timeline sincronizada (barra de espaço).\n"
                                 "Use M (mudo) e S (solo) nas faixas para comparar.")
        self.btn_play.clicked.connect(self.on_play_toggle)
        zl.addWidget(self.btn_play)
        self.time_label = QLabel("00:00:00:00")
        self.time_label.setObjectName("timeLabel")
        zl.addWidget(self.time_label)
        zl.addSpacing(12)
        self.hint = QLabel("Arraste clipes para reagrupar ou ajustar a posição · ← → move 1 "
                           "quadro (Shift = 10) · Ctrl+Z desfaz · botão direito no nome da "
                           "faixa = opções · Ctrl + roda = zoom")
        self.hint.setObjectName("hint")
        zl.addWidget(self.hint, 1)
        for txt, f in (("−", 0.8), ("+", 1.25)):
            b = QPushButton(txt)
            b.setObjectName("zoomButton")
            b.setFixedWidth(30)
            b.clicked.connect(lambda _=False, f=f: self.timeline.canvas.zoom(f))
            zl.addWidget(b)
        fit = QPushButton("Ajustar")
        fit.setObjectName("zoomButton")
        fit.clicked.connect(self.on_fit)
        zl.addWidget(fit)

        # ---- timeline
        self.timeline = TimelineView()
        cv = self.timeline.canvas
        cv.clipMoved.connect(self.on_clip_moved)
        cv.trackMenu.connect(self.on_track_menu)
        cv.clipMenu.connect(self.on_clip_menu)
        cv.trackRename.connect(self.on_rename_track)
        cv.filesDropped.connect(self.add_paths)
        cv.deleteRequested.connect(self.on_remove_selected)
        cv.clipSelected.connect(self.select_in_tree)
        cv.clipAdjusted.connect(self.on_clip_adjusted)
        cv.nudgeRequested.connect(self.on_nudge)
        cv.seekRequested.connect(self.on_seek)
        cv.mixChanged.connect(lambda: self.player.set_active(cv.active_tracks()))
        self.player = Player()
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(33)
        self.play_timer.timeout.connect(self._on_play_tick)
        sc = QShortcut(QKeySequence(Qt.Key_Space), self)
        sc.activated.connect(self.on_play_toggle)

        self.strip = StatusStrip()
        self.strip.cancelRequested.connect(self.on_cancel)

        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(top)
        cl.addWidget(zoom)
        cl.addWidget(self.timeline, 1)
        cl.addWidget(self.strip)
        self.setCentralWidget(central)

        # ---- painel de detalhes
        self.tree = ClipTree()
        self.tree.clipsMoved.connect(lambda uids, t: [self.on_clip_moved(u, t) for u in uids])
        self.tree.itemDoubleClicked.connect(self.on_tree_double_click)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        mono = QFont("Menlo" if sys.platform == "darwin" else "Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.log_view.setFont(mono)
        tabs = QTabWidget()
        tabs.addTab(self.tree, "Lista de clipes")
        tabs.addTab(self.log_view, "Registro")
        self.dock = QDockWidget("Detalhes", self)
        self.dock.setObjectName("detailsDock")
        self.dock.setWidget(tabs)
        self.dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.dock)
        self.dock.setVisible(False)
        self.btn_details.toggled.connect(self.dock.setVisible)
        self.dock.visibilityChanged.connect(self.btn_details.setChecked)

        # ---- menus
        mb = self.menuBar()
        m = mb.addMenu("&Arquivo")
        self.a_files = m.addAction("Adicionar arquivos…", self.on_add_files, QKeySequence("Ctrl+O"))
        self.a_folder = m.addAction("Adicionar pasta…", self.on_add_folder,
                                    QKeySequence("Ctrl+Shift+O"))
        m.addSeparator()
        m.addAction("Abrir projeto…", self.on_open_project)
        m.addAction("Salvar projeto", self.on_save_project, QKeySequence.Save)
        m.addAction("Salvar projeto como…", self.on_save_project_as)
        m.addSeparator()
        m.addAction("Limpar tudo", self.on_clear)
        m.addAction("Sair", self.close, QKeySequence.Quit)
        m = mb.addMenu("E&ditar")
        self.a_undo = m.addAction("Desfazer ajuste manual", self.on_undo, QKeySequence.Undo)
        self.a_undo.setEnabled(False)
        m.addSeparator()
        m.addAction("Remover clipe selecionado", self.on_remove_selected)
        m = mb.addMenu("&Sincronia")
        self.a_sync = m.addAction("Sincronizar", self.on_sync, QKeySequence("Ctrl+Return"))
        m.addAction("Cancelar", self.on_cancel, QKeySequence("Esc"))
        m.addSeparator()
        m.addAction("Nova faixa…", self.on_new_track)
        m.addAction("Opções…", self.on_options, QKeySequence("Ctrl+,"))
        m = mb.addMenu("&Exportar")
        self.export_actions = [
            m.addAction("Premiere Pro (XML)…", lambda: self.do_export("premiere")),
            m.addAction("Final Cut Pro (FCPXML)…", lambda: self.do_export("fcpx")),
            m.addAction("DaVinci Resolve (FCPXML)…", lambda: self.do_export("resolve")),
            m.addAction("Clipe multicâmera — Final Cut / Resolve…",
                        lambda: self.do_export("multicam")),
            m.addAction("Vídeos com áudio do gravador…", lambda: self.do_export("media")),
            m.addAction("Somente WAVs com drift corrigido…", self.on_export_drift_only),
            m.addAction("Relatório CSV…", lambda: self.do_export("csv")),
            m.addAction("Relatório JSON…", self.on_export_json),
        ]
        m = mb.addMenu("E&xibir")
        m.addAction("Ajustar timeline à janela", self.on_fit, QKeySequence("Ctrl+0"))
        m.addAction("Aproximar", lambda: self.timeline.canvas.zoom(1.25), QKeySequence.ZoomIn)
        m.addAction("Afastar", lambda: self.timeline.canvas.zoom(0.8), QKeySequence.ZoomOut)
        da = self.dock.toggleViewAction()
        da.setText("Painel de detalhes")
        m.addAction(da)
        m = mb.addMenu("A&juda")
        m.addAction("Como usar", self.on_help)
        m.addAction("FFmpeg em uso", lambda: QMessageBox.information(self, "FFmpeg",
                                                                       describe_tools()))
        m.addAction(f"Sobre o {APP_NAME}", self.on_about)

    def _step_button(self, text: str, slot) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("stepButton")
        b.setMinimumSize(210, 44)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _set_primary(self, which: Optional[QPushButton]) -> None:
        for b in (self.btn_add, self.btn_sync, self.btn_export):
            b.setProperty("primary", b is which)
            b.style().unpolish(b)
            b.style().polish(b)

    # ------------------------------------------------------------- settings
    def _load_settings(self) -> None:
        s = self.settings
        self.opts = {
            "group": s.value("group", "auto"),
            "clock": s.value("clock", "video"),
            "method": s.value("method", "audio"),
            "fps": s.value("fps", "Automático"),
            "min_score": float(s.value("min_score", SyncOptions.min_score)),
            "fcpxml_version": s.value("fcpxml_version", "1.10"),
            "same_track": s.value("same_track", "false") == "true",
            "use_clock": s.value("use_clock", "true") == "true",
        }
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def _save_settings(self) -> None:
        for k, v in self.opts.items():
            self.settings.setValue(k, ("true" if v else "false") if isinstance(v, bool) else v)
        self.settings.setValue("geometry", self.saveGeometry())

    def closeEvent(self, ev) -> None:  # noqa: N802
        self.player.pause()
        if self.busy():
            self.task.cancel()
            self.task.wait(3000)
        self._save_settings()
        super().closeEvent(ev)

    def last_dir(self) -> str:
        return self.settings.value("last_dir", str(Path.home()))

    def set_last_dir(self, path: str) -> None:
        p = Path(path)
        self.settings.setValue("last_dir", str(p if p.is_dir() else p.parent))

    def timeline_fps(self):
        txt = self.opts["fps"]
        return None if txt.startswith("Auto") else parse_fps_string(txt)

    # -------------------------------------------------------------- tarefas
    def log(self, msg: str) -> None:
        self.log_view.appendPlainText(msg.rstrip())

    def busy(self) -> bool:
        return self.task is not None and self.task.isRunning()

    def set_busy(self, on: bool) -> None:
        for w in (self.btn_add, self.btn_sync, self.btn_export, self.btn_opts):
            w.setEnabled(not on)
        for a in (self.a_files, self.a_folder, self.a_sync, *self.export_actions):
            a.setEnabled(not on)
        self.timeline.canvas.setAcceptDrops(not on)

    def start_task(self, fn, on_done, title: str, on_progress=None) -> None:
        if self.busy():
            return
        t = TaskThread(fn, self)

        def prog(f, m):
            self.strip.progress(f, m)
            if on_progress:
                on_progress()
        t.progressed.connect(prog)
        t.logged.connect(self.log)

        def ok(res):
            self.set_busy(False)
            on_done(res)

        def fail(msg):
            self.set_busy(False)
            self.refresh()
            self.strip.result("✖ Erro: " + msg, ok=False)
            QMessageBox.critical(self, APP_NAME, msg)

        def cancelled():
            self.set_busy(False)
            self.refresh()
            self.strip.idle("Cancelado.")

        t.succeeded.connect(ok)
        t.failed.connect(fail)
        t.cancelled.connect(cancelled)
        self.task = t
        self.set_busy(True)
        self.strip.progress(0.0, title)
        t.start()

    def on_cancel(self) -> None:
        if self.busy():
            self.task.cancel()
            self.strip.idle("Cancelando…")

    # ------------------------------------------------------------ refresh
    def refresh(self, fit: bool = False) -> None:
        if self.player.playing:
            self.player.pause()
        self.timeline.set_project(self.project, self.timeline_fps(), fit=fit)
        self._refresh_tree()
        clips = self.project.all_clips()
        synced = [c for c in clips if c.is_synced]
        failed = [c for c in clips if c.status in FAILED]
        for a in self.export_actions:
            a.setEnabled(bool(synced) and not self.busy())
        if not clips:
            self._set_primary(self.btn_add)
        elif not synced:
            self._set_primary(self.btn_sync)
        else:
            self._set_primary(self.btn_export)
        self.btn_export.setEnabled(bool(synced) and not self.busy())
        self.btn_sync.setEnabled(bool(clips) and not self.busy())
        if self.busy():
            return
        if not clips:
            self.strip.idle("Arraste as pastas das câmeras e gravadores para começar.")
        elif failed and synced:
            self.strip.result(f"✖ {len(failed)} clipe(s) NÃO sincronizado(s) — "
                              f"{len(synced)}/{len(clips)} sincronizados", ok=False)
        elif failed and not synced:
            self.strip.result(f"✖ Nenhum clipe sincronizado ({len(failed)} com problema)",
                              ok=False)
        elif synced:
            est = sum(1 for c in synced if c.estimated)
            extra = f" ({est} sem áudio em comum, posicionados pelo horário)" if est else ""
            self.strip.result(f"✔ {len(synced)}/{len(clips)} clipes sincronizados{extra}",
                              ok=True)
        else:
            ready = sum(1 for c in clips if c.prep >= 1.0)
            self.strip.idle(f"{len(clips)} arquivo(s) em {len(self.project.tracks)} faixa(s) — "
                            f"{ready} pronto(s). Clique em Sincronizar.")

    def _refresh_tree(self) -> None:
        self.tree.clear()
        ref = self.project.reference_track()
        synced = [c for c in self.project.all_clips() if c.is_synced]
        base = min((c.placement() for c in synced), default=0.0)
        fps = self.timeline_fps() or guess_fps(self.project)
        bold = QFont(self.tree.font())
        bold.setBold(True)
        for t in self.project.tracks:
            n_fail = sum(1 for c in t.clips if c.status in FAILED)
            head = "★ referência" if ref and t.name == ref.name else f"{len(t.clips)} arquivo(s)"
            if n_fail:
                head += f" — ✖ {n_fail} não sincronizado(s)"
            ti = QTreeWidgetItem([t.name, "Vídeo" if t.has_video else "Áudio",
                                  fmt_dur(t.total_duration), "", "", "", head])
            ti.setData(0, Qt.UserRole, ("track", t.name))
            for col in range(len(COLS)):
                ti.setFont(col, bold)
            ti.setForeground(6, QBrush(QColor(RED if n_fail else YELLOW)))
            self.tree.addTopLevelItem(ti)
            for c in t.clips:
                kind = "—"
                if c.info:
                    kind = (f"Vídeo {c.info.height}p {fps_label(c.info.fps)}" if c.info.has_video
                            else f"Áudio {c.info.audio_rate / 1000:g} kHz")
                pos = seconds_to_tc(c.placement() - base, fps) if c.is_synced else ""
                conf = f"{c.confidence:.0f}" if c.status == Status.SYNCED else ""
                drift = (f"{c.drift * 1e6:+.1f} ppm" if c.drift_reliable and abs(c.drift) > 5e-7
                         else "")
                status = c.status + (f" — {c.message}" if c.message else "")
                failed = c.status in FAILED
                ci = QTreeWidgetItem([("✖  " if failed else "") + c.name, kind,
                                      fmt_dur(c.duration), pos, conf, drift, status])
                ci.setData(0, Qt.UserRole, ("clip", c.uid))
                ci.setToolTip(0, c.path)
                ci.setForeground(6, QBrush(QColor(STATUS_COLORS.get(c.status, "#8E8E8E"))))
                if failed:
                    for col in range(len(COLS)):
                        ci.setForeground(col, QBrush(QColor(RED)))
                        ci.setBackground(col, QBrush(QColor(RED_BG)))
                ti.addChild(ci)
            ti.setExpanded(True)

    def select_in_tree(self, uid: int) -> None:
        for i in range(self.tree.topLevelItemCount()):
            ti = self.tree.topLevelItem(i)
            for k in range(ti.childCount()):
                if ti.child(k).data(0, Qt.UserRole) == ("clip", uid):
                    self.tree.setCurrentItem(ti.child(k))
                    return

    def on_fit(self) -> None:
        self.timeline.canvas._auto_fit = True
        self.timeline.canvas.fit(self.timeline.viewport().width())

    # --------------------------------------------------------- adicionar
    def on_add_menu(self) -> None:
        m = QMenu(self)
        m.addAction("Adicionar arquivos…", self.on_add_files)
        m.addAction("Adicionar pasta…", self.on_add_folder)
        m.exec(self.btn_add.mapToGlobal(self.btn_add.rect().bottomLeft()))

    def on_add_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(MEDIA_EXTS))
        exts += " " + exts.upper()
        files, _ = QFileDialog.getOpenFileNames(self, "Adicionar arquivos", self.last_dir(),
                                                f"Mídia ({exts});;Todos (*)")
        if files:
            self.add_paths(files)

    def on_add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Adicionar pasta", self.last_dir())
        if d:
            self.add_paths([d])

    def add_paths(self, paths: list, target_track=None) -> None:
        if self.busy():
            return
        mode = "single" if target_track else self.opts["group"]
        added = self.project.add_paths(paths, mode)
        if target_track:
            for c in added:
                self.project.move_clip(c, target_track)
            self.project.remove_empty_tracks()
        if not added:
            self.strip.idle("Nenhum arquivo de mídia novo encontrado.")
            return
        self.log(f"{len(added)} arquivo(s) adicionado(s).")
        self.set_last_dir(paths[0])
        self.project.reset_results()
        self.refresh(fit=True)
        self.prepare_pending()

    def prepare_pending(self) -> None:
        """Lê metadados e prepara o áudio (forma de onda + features) em segundo plano."""
        todo = [c for c in self.project.all_clips()
                if c.status != Status.ERROR and (c.info is None or c.prep < 1.0
                                                  or (c.info.has_audio and c.wave is None))]
        if not todo:
            return
        cache = self.cache_dir

        def work(progress, log, cancel):
            total = len(todo)

            def report():
                done = sum(c.prep for c in todo)
                progress(done / total, f"Preparando mídia ({sum(c.prep >= 1 for c in todo)}"
                                       f"/{total})")

            def one(c):
                if cancel.is_set():
                    return
                try:
                    tr = self.project.track_of(c)
                    prepare_clip(c, cache, cancel=cancel, progress=lambda _f: report(),
                                 channel=tr.audio_channel if tr else None)
                except Exception as e:  # noqa: BLE001
                    if cancel.is_set():
                        return
                    c.status = Status.ERROR
                    c.message = str(e)
                    c.prep = 1.0
                    log(f"Erro em {c.name}: {e}")
                report()
            with ThreadPoolExecutor(4) as ex:
                list(ex.map(one, todo))

        self._probed = sum(1 for c in self.project.all_clips() if c.info)
        self.start_task(work, lambda _: self.refresh(fit=True), "Preparando mídia",
                        on_progress=self._on_prepare_progress)

    def _on_prepare_progress(self) -> None:
        probed = sum(1 for c in self.project.all_clips() if c.info)
        if probed != self._probed:          # durações novas: refaz o layout
            self._probed = probed
            self.timeline.set_project(self.project, self.timeline_fps(), fit=False)
        else:
            self.timeline.canvas.update()

    # ------------------------------------------------------------ edição
    def on_clip_moved(self, uid: int, track: str) -> None:
        c = self.project.clip_by_uid(uid)
        if c is None:
            return
        if not track:
            base = "Câmera" if c.has_video else "Gravador"
            track = self.project.add_track(base).name
        self.project.move_clip(c, track)
        self.project.remove_empty_tracks()
        if any(x.is_synced for x in self.project.all_clips()):
            self.project.reset_results()
            self.log("Faixas alteradas — sincronize novamente.")
        self.refresh(fit=True)

    # ---------------------------------------------------------------- player
    def _play_sources(self) -> list:
        cv = self.timeline.canvas
        out = []
        for b in cv.blocks:
            if cv.rows[b.row].fail or not b.clip.has_audio:
                continue
            tr = self.project.track_of(b.clip)
            pcm_path = cache_paths(b.clip, self.cache_dir, tr.audio_channel if tr else None)[0]
            if pcm_path.exists():
                out.append(Source(tr.name if tr else "", b.start, open_pcm(pcm_path)))
        return out

    def on_play_toggle(self) -> None:
        cv = self.timeline.canvas
        if self.player.playing:
            self.player.pause()
            return
        if not cv.synced_mode or self.busy():
            self.strip.idle("Sincronize primeiro para ouvir a timeline.")
            return
        ok, why = Player.available()
        if not ok:
            QMessageBox.warning(self, APP_NAME, f"Saída de áudio indisponível: {why}")
            return
        self.player.load(self._play_sources())
        self.player.set_active(cv.active_tracks())
        start = cv.playhead or 0.0
        if start >= self.player.end - 0.05:
            start = 0.0
        self.player.seek(start)
        try:
            self.player.play()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Não foi possível tocar o áudio: {e}")
            return
        self.play_timer.start()
        self.btn_play.setText("❚❚  Pausar")

    def on_seek(self, t: float) -> None:
        self.player.seek(t)
        self.timeline.canvas.playhead = t
        self.time_label.setText(seconds_to_tc(t, self.timeline.canvas.fps))
        self.timeline.canvas.update()

    def _on_play_tick(self) -> None:
        cv = self.timeline.canvas
        cv.playhead = self.player.position
        self.time_label.setText(seconds_to_tc(cv.playhead, cv.fps))
        cv.ensure_visible(cv.playhead)
        cv.update()
        if not self.player.playing:
            self.play_timer.stop()
            self.btn_play.setText("▶  Ouvir")

    # ---------------------------------------------------------- ajuste manual
    def _snapshot(self, c) -> dict:
        t = self.project.track_of(c)
        return {"uid": c.uid, "track": t.name if t else None, "offset": c.offset,
                "drift": c.drift, "drift_reliable": c.drift_reliable, "status": c.status,
                "message": c.message, "confidence": c.confidence}

    def on_clip_adjusted(self, uid: int, t_new: float, target: str) -> None:
        c = self.project.clip_by_uid(uid)
        if c is None or self.busy():
            return
        self._undo.append(self._snapshot(c))
        self.a_undo.setEnabled(True)
        if target == "__new__":
            target = self.project.add_track("Câmera" if c.has_video else "Gravador").name
        if target:
            self.project.move_clip(c, target)
            self.project.remove_empty_tracks()
        if c.offset is None or c.status in FAILED:
            c.drift, c.drift_reliable = 0.0, False
        placement = t_new - self.timeline.canvas.shift
        c.offset = placement - c.drift * c.duration / 2
        if c.status != Status.REFERENCE:
            c.status = Status.SYNCED
            c.message = "ajuste manual"
        self.log(f"Ajuste manual: {c.name} → "
                 f"{seconds_to_tc(max(0.0, t_new), self.timeline.canvas.fps)}")
        self.refresh()

    def on_nudge(self, uid: int, frames: int) -> None:
        cv = self.timeline.canvas
        blk = next((b for b in cv.blocks if b.clip.uid == uid), None)
        if blk is not None and not cv.rows[blk.row].fail:
            self.on_clip_adjusted(uid, blk.start + frames / float(cv.fps), "")

    def on_undo(self) -> None:
        if not self._undo:
            return
        st = self._undo.pop()
        c = self.project.clip_by_uid(st["uid"])
        if c is not None:
            if st["track"] and (self.project.track_of(c) is None or
                                self.project.track_of(c).name != st["track"]):
                self.project.move_clip(c, st["track"])
                self.project.remove_empty_tracks()
            for k in ("offset", "drift", "drift_reliable", "status", "message", "confidence"):
                setattr(c, k, st[k])
            self.log(f"Desfeito: {c.name}")
        self.a_undo.setEnabled(bool(self._undo))
        self.refresh()

    def on_rename_track(self, name: str) -> None:
        new, ok = QInputDialog.getText(self, "Renomear faixa", "Nome da faixa:", text=name)
        if ok and new.strip():
            self.project.rename_track(name, new.strip())
            self.refresh()

    def on_track_menu(self, name: str, gpos) -> None:
        m = QMenu(self)
        m.addAction("★  Definir como referência", lambda: self._set_ref(name))
        m.addAction("Renomear…", lambda: self.on_rename_track(name))
        t = self.project.track(name)
        n_ch = max((channel_count(c.info) for c in t.clips if c.info and c.info.has_audio),
                   default=0) if t else 0
        if n_ch > 1:
            cm = m.addMenu("Canal de áudio para análise")
            opts = [(None, "Todos (mistura)")] + [(k, f"Canal {k}") for k in range(1, n_ch + 1)]
            for val, label in opts:
                a = cm.addAction(label, lambda v=val: self._set_channel(name, v))
                a.setCheckable(True)
                a.setChecked(t.audio_channel == val)
        m.addSeparator()
        m.addAction("Remover faixa", lambda: self._remove_track(name))
        m.exec(gpos)

    def _set_channel(self, name: str, channel) -> None:
        t = self.project.track(name)
        if t is None or t.audio_channel == channel:
            return
        t.audio_channel = channel
        for c in t.clips:
            c.wave, c.prep = None, 0.0
        self.project.reset_results()
        self.log(f"Faixa {name}: análise pelo " + (f"canal {channel}." if channel else
                                                    "áudio misturado."))
        self.refresh()
        self.prepare_pending()

    def on_clip_menu(self, uid: int, gpos) -> None:
        c = self.project.clip_by_uid(uid)
        if c is None:
            return
        m = QMenu(self)
        mv = m.addMenu("Mover para faixa")
        cur = self.project.track_of(c)
        for t in self.project.tracks:
            a = mv.addAction(t.name, lambda n=t.name: self.on_clip_moved(uid, n))
            a.setEnabled(t is not cur)
        mv.addSeparator()
        mv.addAction("Nova faixa", lambda: self.on_clip_moved(uid, ""))
        m.addAction("Mostrar no Finder" if sys.platform == "darwin" else "Mostrar na pasta",
                    lambda: reveal_in_os(c.path))
        m.addAction("Abrir arquivo", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(c.path)))
        m.addSeparator()
        m.addAction("Remover do projeto", lambda: self._remove_clip(uid))
        m.exec(gpos)

    def _set_ref(self, name: str) -> None:
        self.project.reference = name
        if any(c.is_synced for c in self.project.all_clips()):
            self.log(f"Referência: {name} — sincronize novamente para aplicar.")
        self.refresh()

    def _remove_track(self, name: str) -> None:
        t = self.project.track(name)
        if t:
            for c in list(t.clips):
                self.project.remove_clip(c)
        self.project.remove_empty_tracks()
        self.refresh(fit=True)

    def _remove_clip(self, uid: int) -> None:
        c = self.project.clip_by_uid(uid)
        if c:
            self.project.remove_clip(c)
            self.project.remove_empty_tracks()
            self.refresh(fit=True)

    def on_remove_selected(self) -> None:
        if self.timeline.canvas.selected is not None and not self.busy():
            self._remove_clip(self.timeline.canvas.selected)

    def on_new_track(self) -> None:
        name, ok = QInputDialog.getText(self, "Nova faixa", "Nome (câmera ou gravador):",
                                        text=self.project.unique_track_name("Câmera"))
        if ok and name.strip():
            self.project.add_track(name.strip())
            self.refresh()

    def on_clear(self) -> None:
        if self.project.all_clips() and QMessageBox.question(
                self, APP_NAME, "Remover todos os arquivos do projeto?") != QMessageBox.Yes:
            return
        self.project.clear()
        self.project_path = None
        self._undo.clear()
        self.refresh(fit=True)

    def on_tree_double_click(self, item, _col) -> None:
        kind, val = item.data(0, Qt.UserRole)
        if kind == "clip":
            c = self.project.clip_by_uid(val)
            if c:
                QDesktopServices.openUrl(QUrl.fromLocalFile(c.path))
        else:
            self.on_rename_track(val)

    def on_options(self) -> None:
        dlg = OptionsDialog(self)
        if dlg.exec() == QDialog.Accepted:
            dlg.apply(self)
            self._save_settings()
            self.refresh()

    # ---------------------------------------------------------- sincronizar
    def on_sync(self) -> None:
        if self.busy() or not self.project.all_clips():
            return
        if self._undo and QMessageBox.question(
                self, APP_NAME, "Sincronizar de novo desfaz os ajustes manuais. Continuar?") \
                != QMessageBox.Yes:
            return
        self._undo.clear()
        self.a_undo.setEnabled(False)
        o = self.opts
        opts = SyncOptions(min_score=o["min_score"], compare_same_track=o["same_track"],
                           timeline_clock=o["clock"], method=o["method"],
                           use_clock=o["use_clock"],
                           cache_dir=self.cache_dir)
        self.log("\n— Sincronização iniciada —")

        def work(progress, log, cancel):
            return SyncEngine(self.project, opts, progress=progress, log=log,
                              cancel=cancel).run()

        def done(rep):
            self.refresh(fit=True)
            if rep.warnings:
                self.log("Avisos:\n  " + "\n  ".join(rep.warnings))

        self.start_task(work, done, "Sincronizando",
                        on_progress=lambda: self.timeline.canvas.update())

    # ------------------------------------------------------------- exportar
    def on_export(self) -> None:
        if not any(c.is_synced for c in self.project.all_clips()):
            return
        dlg = ExportDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        target = dlg.target()
        self.settings.setValue("export_target", target)
        self.settings.setValue("drift_fix", "true" if dlg.drift.isChecked() else "false")
        self.settings.setValue("mute_camera_audio", "true" if dlg.mute.isChecked() else "false")
        self.do_export(target, dlg.drift.isChecked(), dlg.unsynced.isChecked(),
                       dlg.mute.isChecked())

    def do_export(self, target: str, drift: Optional[bool] = None,
                  unsynced: bool = False, mute: Optional[bool] = None) -> None:
        if drift is None:
            drift = self.settings.value("drift_fix", "false") == "true"
        if mute is None:
            mute = self.settings.value("mute_camera_audio", "false") == "true"
        ver = self.opts["fcpxml_version"]
        if target == "premiere":
            path = self._save_path("Exportar para o Premiere Pro", ".xml", "XML (*.xml)")
            if path:
                self._export_timeline(lambda tl, p: export_xmeml(tl, p, mute_camera_audio=mute),
                                      path, drift, unsynced)
        elif target in ("fcpx", "resolve"):
            path = self._save_path("Exportar FCPXML", ".fcpxml", "FCPXML (*.fcpxml)")
            if path:
                self._export_timeline(lambda tl, p: export_fcpxml(
                    tl, p, version=ver, mute_camera_audio=mute), path, drift, unsynced)
        elif target == "multicam":
            path = self._save_path("Exportar clipe multicâmera", ".fcpxml", "FCPXML (*.fcpxml)")
            if path:
                ref = self.project.reference_track()
                audio = ref.name if ref and not ref.has_video else None
                self._export_timeline(lambda tl, p: export_fcpxml_multicam(
                    tl, p, version=ver, audio_track=audio), path, drift, False)
        elif target == "media":
            self.on_export_replace()
        elif target == "csv":
            path = self._save_path("Exportar CSV", ".csv", "CSV (*.csv)")
            if path:
                export_csv(self.project, path, self.timeline_fps() or guess_fps(self.project))
                self.strip.result(f"✔ Exportado: {Path(path).name}", ok=True)
                reveal_in_os(path)

    def _save_path(self, title: str, suffix: str, filt: str) -> Optional[str]:
        base = Path(self.last_dir()) / (self._job_name() + suffix)
        path, _ = QFileDialog.getSaveFileName(self, title, str(base), filt)
        if not path:
            return None
        if not path.lower().endswith(suffix):
            path += suffix
        self.set_last_dir(path)
        return path

    def _job_name(self) -> str:
        if self.project_path:
            return Path(self.project_path).stem
        clips = self.project.all_clips()
        if clips:
            return Path(clips[0].path).parent.parent.name or "Mega Sinc"
        return "Mega Sinc"

    def _export_timeline(self, writer, path: str, drift: bool, unsynced: bool) -> None:
        fps = self.timeline_fps()
        name = Path(path).stem

        def write(corrected):
            tl = build_timeline(self.project, fps, name, corrected, unsynced)
            try:
                writer(tl, path)
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, APP_NAME, f"Falha ao exportar: {e}")
                return
            self.log(f"Exportado: {path}")
            extra = f" ({len(corrected)} áudio(s) com drift corrigido)" if corrected else ""
            self.strip.result(f"✔ Timeline exportada: {Path(path).name}{extra}", ok=True)
            reveal_in_os(path)

        if drift and drift_candidates(self.project):
            out_dir = str(Path(path).with_name(Path(path).stem + "_drift_corrigido"))
            self.start_task(lambda pr, lg, cn: export_drift_corrected(
                self.project, out_dir, progress=pr, cancel=cn, log=lg), write,
                "Corrigindo drift do áudio")
        else:
            write({})

    def on_export_json(self) -> None:
        path = self._save_path("Exportar JSON", ".json", "JSON (*.json)")
        if path:
            export_json(self.project, path, self.timeline_fps() or guess_fps(self.project))
            self.strip.result(f"✔ Exportado: {Path(path).name}", ok=True)

    def on_export_replace(self) -> None:
        tracks = [t.name for t in self.project.tracks
                  if any(c.is_synced and c.has_audio and not c.has_video for c in t.clips)]
        tracks = tracks or [t.name for t in self.project.tracks
                            if any(c.is_synced and c.has_audio for c in t.clips)]
        if not tracks:
            return
        ref = self.project.reference_track()
        dlg = ReplaceAudioDialog(tracks, ref.name if ref else None, self)
        if dlg.exec() != QDialog.Accepted:
            return
        out = QFileDialog.getExistingDirectory(self, "Pasta de destino", self.last_dir())
        if not out:
            return
        master, keep = dlg.track.currentText(), dlg.keep.isChecked()

        def done(files):
            self.strip.result(f"✔ {len(files)} vídeo(s) exportado(s) em {Path(out).name}",
                              ok=bool(files))
            if files:
                reveal_in_os(files[0])

        self.start_task(lambda pr, lg, cn: export_replaced_audio(
            self.project, master, out, keep, progress=pr, cancel=cn, log=lg), done,
            "Exportando vídeos")

    def on_export_drift_only(self) -> None:
        if not drift_candidates(self.project):
            QMessageBox.information(self, APP_NAME, "Nenhum áudio com drift relevante "
                                    "(> 2 ms ao longo do arquivo).")
            return
        out = QFileDialog.getExistingDirectory(self, "Pasta de destino", self.last_dir())
        if out:
            self.start_task(lambda pr, lg, cn: export_drift_corrected(
                self.project, out, progress=pr, cancel=cn, log=lg),
                lambda res: self.strip.result(f"✔ {len(res)} WAV(s) gerado(s)", ok=True),
                "Corrigindo drift")

    # -------------------------------------------------------------- projeto
    def on_open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Abrir projeto", self.last_dir(),
                                              "Projeto Mega Sinc (*.megasinc)")
        if path:
            self.load_project(path)

    def load_project(self, path: str) -> None:
        try:
            self.project = Project.load(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Não foi possível abrir: {e}")
            return
        self.project_path = path
        missing = [c.name for c in self.project.all_clips() if not os.path.exists(c.path)]
        if missing:
            QMessageBox.warning(self, APP_NAME, "Arquivos não encontrados:\n" +
                                "\n".join(missing[:15]))
        self.refresh(fit=True)
        self.prepare_pending()

    def on_save_project(self) -> None:
        if not self.project_path:
            return self.on_save_project_as()
        self.project.save(self.project_path)
        self.strip.idle(f"Projeto salvo: {Path(self.project_path).name}")

    def on_save_project_as(self) -> None:
        path = self._save_path("Salvar projeto", ".megasinc", "Projeto Mega Sinc (*.megasinc)")
        if path:
            self.project_path = path
            self.on_save_project()

    # ---------------------------------------------------------------- ajuda
    def on_help(self) -> None:
        QMessageBox.information(self, "Como usar", (
            "1. ＋ Adicionar mídia (ou arraste as pastas para a janela). Cada câmera ou "
            "gravador vira uma faixa; arraste clipes entre faixas para corrigir.\n\n"
            "2. ▶ Sincronizar.\n\n"
            "3. ⇪ Exportar timeline para Premiere Pro, Final Cut Pro ou DaVinci Resolve — "
            "ou gere vídeos já com o áudio do gravador.\n\n"
            "Clipes em vermelho não sincronizaram: confira se o áudio deles é do mesmo "
            "evento, ou reduza a 'Confiança mínima' em ⚙ Opções.\n\n"
            "Botão direito no nome de uma faixa → Definir como referência."))

    def on_about(self) -> None:
        QMessageBox.about(self, APP_NAME, (
            f"<b>{APP_NAME} {__version__}</b><br><br>"
            "Sincronização automática de múltiplas câmeras e gravadores pelo áudio.<br><br>"
            "Usa FFmpeg (ffmpeg.org) para leitura de mídia."))


def apply_dark_theme(app: QApplication) -> None:
    """Tema Mega Sinc: tons escuros com destaque amarelo."""
    from PySide6.QtGui import QPalette
    app.setStyle("Fusion")
    p = QPalette()
    text = QColor("#ECECEC")
    p.setColor(QPalette.Window, QColor("#141414"))
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, QColor("#1a1a1a"))
    p.setColor(QPalette.AlternateBase, QColor("#202020"))
    p.setColor(QPalette.ToolTipBase, QColor("#222222"))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, QColor("#242424"))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(YELLOW))
    p.setColor(QPalette.Highlight, QColor(YELLOW))
    p.setColor(QPalette.HighlightedText, QColor("#111111"))
    p.setColor(QPalette.Link, QColor(YELLOW))
    p.setColor(QPalette.PlaceholderText, QColor("#8E8E8E"))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor("#5f5f5f"))
    app.setPalette(p)
    app.setStyleSheet(f"""
        QMainWindow, QDialog {{ background: #141414; }}
        QToolTip {{ color: #ECECEC; background: #222222; border: 1px solid {YELLOW}; }}
        QMenuBar {{ background: #0b0b0b; }}
        QMenuBar::item:selected, QMenu::item:selected {{ background: {YELLOW}; color: #111; }}
        QMenu {{ background: #1c1c1c; border: 1px solid #333; }}
        QFrame#topBar {{ background: #000000; border-bottom: 2px solid {YELLOW}; }}
        QFrame#zoomBar {{ background: #0f0f0f; border-bottom: 1px solid #222; }}
        QLabel#hint {{ color: #6f6f6f; }}
        QPushButton#stepButton {{ background: #262626; color: #CFCFCF; border: 1px solid #3a3a3a;
                                  border-radius: 6px; font-size: 15px; font-weight: 600;
                                  padding: 6px 18px; }}
        QPushButton#stepButton:hover {{ border: 1px solid {YELLOW}; color: {YELLOW}; }}
        QPushButton#stepButton[primary="true"] {{ background: {YELLOW}; color: #111;
                                                  border: 1px solid {YELLOW}; }}
        QPushButton#stepButton[primary="true"]:hover {{ background: #FFD54A; color: #111; }}
        QPushButton#stepButton:disabled {{ background: #1c1c1c; color: #4a4a4a;
                                           border: 1px solid #262626; }}
        QPushButton#flatButton {{ background: transparent; color: #BDBDBD; border: none;
                                  padding: 6px 10px; font-size: 13px; }}
        QPushButton#flatButton:hover, QPushButton#flatButton:checked {{ color: {YELLOW}; }}
        QPushButton#zoomButton {{ background: #1f1f1f; border: 1px solid #333; border-radius: 4px;
                                  padding: 2px 8px; }}
        QPushButton#zoomButton:hover {{ border-color: {YELLOW}; color: {YELLOW}; }}
        QPushButton#playButton {{ background: {YELLOW}; color: #111; font-weight: bold;
                                  border: none; border-radius: 4px; padding: 3px 12px; }}
        QPushButton#playButton:hover {{ background: #FFD54A; color: #111; }}
        QLabel#timeLabel {{ color: {YELLOW}; font-family: Menlo, Consolas, monospace;
                            font-size: 13px; padding-left: 8px; }}
        QPushButton#cancelButton {{ background: #111; color: #fff; border: 1px solid #555;
                                    border-radius: 4px; padding: 3px 12px; }}
        QPushButton#primaryButton {{ background: {YELLOW}; color: #111; font-weight: bold;
                                     border: none; border-radius: 5px; padding: 6px 16px; }}
        QRadioButton#exportOption {{ background: #1c1c1c; border: 1px solid #2e2e2e;
                                     border-radius: 6px; padding: 10px; font-size: 13px; }}
        QRadioButton#exportOption:checked {{ border: 1px solid {YELLOW}; background: #2a2410; }}
        QTreeWidget {{ border: 1px solid #2a2a2a; }}
        QHeaderView::section {{ background: #1f1f1f; color: {YELLOW}; border: none;
                               border-right: 1px solid #2a2a2a; padding: 4px 6px; font-weight: bold; }}
        QTreeView::item:selected {{ background: {YELLOW}; color: #111; }}
        QTabBar::tab {{ background: #1a1a1a; color: #aaa; padding: 6px 14px; }}
        QTabBar::tab:selected {{ color: {YELLOW}; border-bottom: 2px solid {YELLOW}; }}
        QDockWidget::title {{ background: #101010; color: {YELLOW}; padding: 4px; }}
        QComboBox, QDoubleSpinBox, QLineEdit {{ background: #1f1f1f; border: 1px solid #333;
                                               border-radius: 5px; padding: 3px 6px; }}
        QComboBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{ border: 1px solid {YELLOW}; }}
        QPushButton {{ background: #242424; border: 1px solid #333; border-radius: 5px;
                       padding: 4px 10px; }}
        QPushButton:hover {{ border: 1px solid {YELLOW}; color: {YELLOW}; }}
        QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
            background: {YELLOW}; border: 1px solid {YELLOW}; border-radius: 3px; }}
        QCheckBox::indicator:unchecked, QRadioButton::indicator:unchecked {{
            background: #1f1f1f; border: 1px solid #444; border-radius: 3px; }}
        QPlainTextEdit {{ background: #0e0e0e; border: none; color: #BDBDBD; }}
        QScrollBar:horizontal, QScrollBar:vertical {{ background: #141414; border: none; }}
        QScrollBar::handle {{ background: #3a3a3a; border-radius: 4px; min-height: 20px;
                              min-width: 20px; }}
        QScrollBar::handle:hover {{ background: {YELLOW}; }}
    """)
