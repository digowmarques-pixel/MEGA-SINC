"""Janela principal."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QFont, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSizePolicy, QSplitter, QStyle, QToolBar, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..engine import SyncEngine, SyncOptions
from ..export import (export_csv, export_drift_corrected, export_fcpxml, export_json,
                      export_replaced_audio, export_xmeml)
from ..export.media_export import drift_candidates
from ..media import MEDIA_EXTS, describe_tools, find_tool, fps_label, parse_fps_string, probe
from ..project import GROUP_MODES, Project, Status
from ..timeline import build_timeline, guess_fps
from .timeline_view import TimelineView
from .workers import TaskThread

COLS = ["Faixa / Arquivo", "Tipo", "Duração", "Posição", "Confiança", "Drift", "Status"]
FPS_CHOICES = ["Automático", "23.976", "24", "25", "29.97", "30", "48", "50", "59.94", "60"]
YELLOW = "#FFC400"
RED = "#FF4D4D"
RED_BG = "#3A1212"
STATUS_COLORS = {
    Status.REFERENCE: YELLOW,
    Status.SYNCED: "#7BD88F",
    Status.UNSYNCED: RED,     # tudo que não sincronizou = vermelho
    Status.ERROR: RED,
    Status.PENDING: "#8E8E8E",
}
FAILED = (Status.UNSYNCED, Status.ERROR)


def play_icon(color: str):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QIcon, QPainter, QPixmap, QPolygonF
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawPolygon(QPolygonF([QPointF(16, 8), QPointF(56, 32), QPointF(16, 56)]))
    p.end()
    return QIcon(pm)


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
class ClipTree(QTreeWidget):
    filesDropped = Signal(list, object)      # caminhos, faixa alvo (ou None)
    clipsMoved = Signal(list, str)           # uids, faixa alvo

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setColumnCount(len(COLS))
        self.setHeaderLabels(COLS)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        h = self.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(COLS)):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        h.setStretchLastSection(False)

    def _target_track(self, pos) -> Optional[str]:
        item = self.itemAt(pos)
        if item is None:
            return None
        if item.parent() is not None:
            item = item.parent()
        kind, val = item.data(0, Qt.UserRole)
        return val if kind == "track" else None

    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls() or ev.source() is self:
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        elif ev.source() is self:
            ev.setDropAction(Qt.MoveAction)
            ev.accept()
        else:
            ev.ignore()

    def dropEvent(self, ev) -> None:  # noqa: N802
        pos = ev.position().toPoint()
        target = self._target_track(pos)
        if ev.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
            ev.acceptProposedAction()
            if paths:
                self.filesDropped.emit(paths, target)
            return
        if ev.source() is self:
            uids = [it.data(0, Qt.UserRole)[1] for it in self.selectedItems()
                    if it.data(0, Qt.UserRole)[0] == "clip"]
            ev.setDropAction(Qt.IgnoreAction)
            ev.accept()
            if uids and target:
                self.clipsMoved.emit(uids, target)
            return
        ev.ignore()


# --------------------------------------------------------------------------
class ReplaceAudioDialog(QDialog):
    def __init__(self, tracks: list[str], default: Optional[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Exportar vídeos com áudio sincronizado")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Cria cópias dos vídeos (sem recodificar a imagem) com o áudio do gravador\n"
            "sincronizado na trilha 1. Os arquivos saem em .mov com áudio PCM 24-bit."))
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
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1280, 820)
        self.setAcceptDrops(True)
        self.project = Project()
        self.settings = QSettings("MegaSinc", "MegaSinc")
        self.task: Optional[TaskThread] = None
        self.project_path: Optional[str] = None
        self._build_ui()
        self._load_settings()
        self.refresh()
        if not find_tool("ffmpeg"):
            QMessageBox.warning(self, APP_NAME, "FFmpeg não foi encontrado.\n\n" + describe_tools())

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        st = self.style()
        tb = QToolBar("Principal")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.addToolBar(tb)

        def act(text, icon, slot, shortcut=None, tip=None):
            a = QAction(st.standardIcon(icon), text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip)
            return a

        self.a_add = act("Arquivos", QStyle.SP_FileIcon, self.on_add_files, "Ctrl+O",
                         "Adicionar arquivos de vídeo/áudio")
        self.a_add_dir = act("Pasta", QStyle.SP_DirIcon, self.on_add_folder, "Ctrl+Shift+O",
                             "Adicionar todos os arquivos de uma pasta")
        self.a_new_track = act("Nova faixa", QStyle.SP_FileDialogNewFolder, self.on_new_track)
        self.a_remove = act("Remover", QStyle.SP_TrashIcon, self.on_remove, "Del",
                            "Remover selecionados")
        self.a_clear = act("Limpar", QStyle.SP_DialogResetButton, self.on_clear)
        self.a_sync = act("Sincronizar", QStyle.SP_MediaPlay, self.on_sync, "Ctrl+Return")
        self.a_sync.setIcon(play_icon(YELLOW))
        self.a_cancel = act("Cancelar", QStyle.SP_BrowserStop, self.on_cancel, "Esc")
        self.a_cancel.setEnabled(False)
        for a in (self.a_add, self.a_add_dir, self.a_new_track, self.a_remove, self.a_clear):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.a_sync)
        tb.addAction(self.a_cancel)
        tb.addSeparator()

        self.export_menu = QMenu(self)
        self.a_fcpxml = self.export_menu.addAction("Final Cut Pro / DaVinci Resolve (FCPXML)…",
                                                   self.on_export_fcpxml)
        self.a_xml = self.export_menu.addAction("Premiere Pro / DaVinci Resolve (XML)…",
                                                self.on_export_xml)
        self.export_menu.addSeparator()
        self.a_replace = self.export_menu.addAction("Vídeos com áudio do gravador (.mov)…",
                                                    self.on_export_replace)
        self.a_driftwav = self.export_menu.addAction("Somente WAVs com drift corrigido…",
                                                     self.on_export_drift_only)
        self.export_menu.addSeparator()
        self.a_csv = self.export_menu.addAction("Relatório CSV…", self.on_export_csv)
        self.a_json = self.export_menu.addAction("Relatório JSON…", self.on_export_json)
        self.export_btn = QToolButton()
        self.export_btn.setText("Exportar")
        self.export_btn.setIcon(st.standardIcon(QStyle.SP_DialogSaveButton))
        self.export_btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.export_btn.setPopupMode(QToolButton.InstantPopup)
        self.export_btn.setMenu(self.export_menu)
        tb.addWidget(self.export_btn)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        from .app import draw_logo
        logo = QLabel()
        logo.setPixmap(draw_logo(40))
        tb.addWidget(logo)
        brand = QLabel(f"<span style='color:{YELLOW}; font-size:20px; font-weight:800;"
                       f" letter-spacing:2px'>MEGA</span><span style='color:#ECECEC;"
                       f" font-size:20px; font-weight:300; letter-spacing:2px'> SINC</span>")
        brand.setContentsMargins(6, 0, 12, 0)
        tb.addWidget(brand)

        # menus
        mb = self.menuBar()
        m_file = mb.addMenu("&Arquivo")
        m_file.addAction("Abrir projeto…", self.on_open_project)
        m_file.addAction("Salvar projeto", self.on_save_project, QKeySequence.Save)
        m_file.addAction("Salvar projeto como…", self.on_save_project_as)
        m_file.addSeparator()
        m_file.addAction(self.a_add)
        m_file.addAction(self.a_add_dir)
        m_file.addSeparator()
        m_file.addAction("Sair", self.close, QKeySequence.Quit)
        mb.addMenu(self.export_menu).setText("&Exportar")
        m_help = mb.addMenu("A&juda")
        m_help.addAction("Como usar", self.on_help)
        m_help.addAction("FFmpeg em uso", lambda: QMessageBox.information(
            self, "FFmpeg", describe_tools()))
        m_help.addAction(f"Sobre o {APP_NAME}", self.on_about)

        # centro
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        split = QSplitter(Qt.Vertical)
        self.tree = ClipTree()
        self.tree.filesDropped.connect(self.on_files_dropped)
        self.tree.clipsMoved.connect(self.on_clips_moved)
        self.tree.itemChanged.connect(self.on_item_changed)
        self.tree.itemSelectionChanged.connect(self.on_tree_selection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.on_context_menu)
        self.tree.itemDoubleClicked.connect(self.on_double_click)
        split.addWidget(self.tree)

        tl_box = QWidget()
        tl_lay = QVBoxLayout(tl_box)
        tl_lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(QLabel(f"<b style='color:{YELLOW}'>Timeline</b>  <span style='color:#8E8E8E'>"
                             "(Ctrl + roda do mouse = zoom)</span>"))
        bar.addStretch()
        for txt, f in (("−", 0.8), ("+", 1.25)):
            b = QPushButton(txt)
            b.setFixedWidth(32)
            b.clicked.connect(lambda _=False, f=f: self.timeline.canvas.zoom(f))
            bar.addWidget(b)
        b = QPushButton("Ajustar")
        b.clicked.connect(lambda: self.timeline.canvas.fit(self.timeline.viewport().width()))
        bar.addWidget(b)
        tl_lay.addLayout(bar)
        self.timeline = TimelineView()
        self.timeline.canvas.clipClicked.connect(self.select_clip)
        tl_lay.addWidget(self.timeline)
        split.addWidget(tl_box)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        f = QFont("Menlo" if sys.platform == "darwin" else "Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(10)
        self.log_view.setFont(f)
        split.addWidget(self.log_view)
        split.setSizes([380, 280, 110])
        root.addWidget(split, 1)
        root.addWidget(self._build_options())
        self.setCentralWidget(central)

        # barra de status
        self.status_label = QLabel("Arraste arquivos ou pastas para começar.")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(320)
        self.progress.setRange(0, 1000)
        self.progress.setVisible(False)
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)

    def _build_options(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(300)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 0, 0, 0)
        title = QLabel(f"<b style='color:{YELLOW}; font-size:15px'>Opções</b>")
        lay.addWidget(title)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft)
        self.cb_group = QComboBox()
        for k, v in GROUP_MODES.items():
            self.cb_group.addItem(v, k)
        self.cb_group.setToolTip("Como novos arquivos são agrupados em faixas (câmeras/gravadores).")
        form.addRow("Agrupar:", self.cb_group)
        self.cb_ref = QComboBox()
        self.cb_ref.setToolTip("Faixa que define o tempo zero (normalmente o gravador de áudio).")
        self.cb_ref.currentIndexChanged.connect(self.on_ref_changed)
        form.addRow("Referência:", self.cb_ref)
        self.cb_clock = QComboBox()
        self.cb_clock.addItem("Câmeras (corrige o áudio)", "video")
        self.cb_clock.addItem("Faixa de referência", "reference")
        self.cb_clock.setToolTip("Relógio usado pela timeline quando há drift entre aparelhos.")
        form.addRow("Relógio:", self.cb_clock)
        self.cb_fps = QComboBox()
        self.cb_fps.setEditable(True)
        self.cb_fps.addItems(FPS_CHOICES)
        self.cb_fps.setToolTip("Quadros por segundo da sequência exportada.")
        form.addRow("FPS da timeline:", self.cb_fps)
        self.sp_score = QDoubleSpinBox()
        self.sp_score.setRange(4.0, 40.0)
        self.sp_score.setSingleStep(0.5)
        self.sp_score.setValue(SyncOptions.min_score)
        self.sp_score.setToolTip("Confiança mínima para aceitar uma correspondência.\n"
                                 "Menor = encontra mais (risco de erro); maior = mais rigoroso.")
        form.addRow("Confiança mín.:", self.sp_score)
        self.cb_fcpx = QComboBox()
        self.cb_fcpx.addItems(["1.10", "1.11", "1.9", "1.8"])
        self.cb_fcpx.setToolTip("1.10/1.11: Final Cut Pro 10.6+ / Resolve 18+.\n"
                                "1.8: versões mais antigas.")
        form.addRow("Versão FCPXML:", self.cb_fcpx)
        lay.addLayout(form)
        self.ck_same = QCheckBox("Comparar clipes da mesma faixa")
        self.ck_same.setToolTip("Útil quando uma câmera gravou clipes sobrepostos "
                                "(ex.: arquivos divididos com overlap).")
        lay.addWidget(self.ck_same)
        self.ck_drift = QCheckBox("Corrigir drift do áudio nos XMLs")
        self.ck_drift.setToolTip("Gera WAVs esticados para o relógio da timeline e usa-os\n"
                                 "nos XMLs. Recomendado em gravações longas (> 10 min).")
        lay.addWidget(self.ck_drift)
        self.ck_unsynced = QCheckBox("Incluir não sincronizados no fim")
        lay.addWidget(self.ck_unsynced)
        lay.addSpacing(12)
        tip = QLabel(
            "<span style='color:#8E8E8E'>"
            f"<b style='color:{YELLOW}'>Dicas</b><br>"
            "• Cada faixa = um aparelho (câmera ou gravador). Arraste clipes entre faixas "
            "para corrigir o agrupamento.<br>"
            "• Clique com o botão direito numa faixa para defini-la como referência ou "
            "renomeá-la.<br>"
            "• O áudio de cada câmera precisa ter captado o mesmo som que o gravador "
            "(mesmo que baixo ou com ruído).</span>")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        lay.addStretch()
        big = QPushButton("  Sincronizar")
        big.setIcon(play_icon("#111111"))
        big.setMinimumHeight(42)
        big.setObjectName("syncButton")
        big.clicked.connect(self.on_sync)
        self.big_sync = big
        lay.addWidget(big)
        return w

    # ------------------------------------------------------------- settings
    def _load_settings(self) -> None:
        s = self.settings
        i = self.cb_group.findData(s.value("group", "auto"))
        self.cb_group.setCurrentIndex(max(0, i))
        i = self.cb_clock.findData(s.value("clock", "video"))
        self.cb_clock.setCurrentIndex(max(0, i))
        self.cb_fps.setCurrentText(s.value("fps", "Automático"))
        self.sp_score.setValue(float(s.value("min_score", SyncOptions.min_score)))
        self.cb_fcpx.setCurrentText(s.value("fcpxml_version", "1.10"))
        self.ck_drift.setChecked(s.value("drift_fix", "false") == "true")
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("group", self.cb_group.currentData())
        s.setValue("clock", self.cb_clock.currentData())
        s.setValue("fps", self.cb_fps.currentText())
        s.setValue("min_score", self.sp_score.value())
        s.setValue("fcpxml_version", self.cb_fcpx.currentText())
        s.setValue("drift_fix", "true" if self.ck_drift.isChecked() else "false")
        s.setValue("geometry", self.saveGeometry())

    def closeEvent(self, ev) -> None:  # noqa: N802
        if self.task and self.task.isRunning():
            self.task.cancel()
            self.task.wait(3000)
        self._save_settings()
        super().closeEvent(ev)

    def last_dir(self) -> str:
        return self.settings.value("last_dir", str(Path.home()))

    def set_last_dir(self, path: str) -> None:
        p = Path(path)
        self.settings.setValue("last_dir", str(p if p.is_dir() else p.parent))

    # -------------------------------------------------------------- helpers
    def log(self, msg: str) -> None:
        self.log_view.appendPlainText(msg.rstrip())

    def busy(self) -> bool:
        return self.task is not None and self.task.isRunning()

    def set_busy(self, on: bool) -> None:
        for a in (self.a_add, self.a_add_dir, self.a_new_track, self.a_remove, self.a_clear,
                  self.a_sync):
            a.setEnabled(not on)
        self.big_sync.setEnabled(not on)
        self.export_btn.setEnabled(not on)
        self.a_cancel.setEnabled(on)
        self.progress.setVisible(on)
        self.tree.setDragEnabled(not on)
        self.tree.setAcceptDrops(not on)
        if not on:
            self.progress.setValue(0)

    def start_task(self, fn, on_done, title: str, on_fail=None) -> None:
        if self.busy():
            return
        t = TaskThread(fn, self)
        t.progressed.connect(lambda f, m: (self.progress.setValue(int(f * 1000)),
                                           self.status_label.setText(m)))
        t.logged.connect(self.log)

        def finished_ok(res):
            self.set_busy(False)
            on_done(res)

        def failed(msg):
            self.set_busy(False)
            self.status_label.setText("Erro: " + msg)
            if on_fail:
                on_fail(msg)
            else:
                QMessageBox.critical(self, APP_NAME, msg)

        def cancelled():
            self.set_busy(False)
            self.status_label.setText("Cancelado.")
            self.refresh()

        t.succeeded.connect(finished_ok)
        t.failed.connect(failed)
        t.cancelled.connect(cancelled)
        self.task = t
        self.set_busy(True)
        self.status_label.setText(title)
        t.start()

    def selected_clips(self):
        out = []
        for it in self.tree.selectedItems():
            kind, val = it.data(0, Qt.UserRole)
            if kind == "clip":
                c = self.project.clip_by_uid(val)
                if c:
                    out.append(c)
        return out

    def selected_tracks(self) -> list[str]:
        return [it.data(0, Qt.UserRole)[1] for it in self.tree.selectedItems()
                if it.data(0, Qt.UserRole)[0] == "track"]

    def timeline_fps(self):
        txt = self.cb_fps.currentText()
        return None if txt.startswith("Auto") else parse_fps_string(txt)

    # ---------------------------------------------------------------- refresh
    def refresh(self) -> None:
        self.tree.blockSignals(True)
        expanded = {self.tree.topLevelItem(i).data(0, Qt.UserRole)[1]
                    for i in range(self.tree.topLevelItemCount())
                    if not self.tree.topLevelItem(i).isExpanded()}
        self.tree.clear()
        ref_track = self.project.reference_track()
        synced = [c for c in self.project.all_clips() if c.is_synced]
        base = min((c.placement() for c in synced), default=0.0)
        fps = self.timeline_fps() or guess_fps(self.project)
        bold = QFont(self.tree.font())
        bold.setBold(True)
        for t in self.project.tracks:
            is_ref = ref_track is not None and t.name == ref_track.name
            n_fail = sum(1 for c in t.clips if c.status in FAILED)
            head = (("★ referência" + ("" if self.project.reference else " (auto)"))
                    if is_ref else f"{len(t.clips)} arquivo(s)")
            if n_fail:
                head += f" — ✖ {n_fail} não sincronizado(s)"
            ti = QTreeWidgetItem([t.name, "Vídeo" if t.has_video else "Áudio",
                                  fmt_dur(t.total_duration), "", "", "", head])
            ti.setData(0, Qt.UserRole, ("track", t.name))
            ti.setFlags(ti.flags() | Qt.ItemIsEditable | Qt.ItemIsDropEnabled)
            ti.setFlags(ti.flags() & ~Qt.ItemIsDragEnabled)
            for col in range(len(COLS)):
                ti.setFont(col, bold)
            if n_fail:
                ti.setForeground(6, QBrush(QColor(RED)))
            elif is_ref:
                ti.setForeground(6, QBrush(QColor(STATUS_COLORS[Status.REFERENCE])))
            self.tree.addTopLevelItem(ti)
            for c in t.clips:
                kind = "—"
                if c.info:
                    kind = (f"Vídeo {c.info.height}p {fps_label(c.info.fps)}" if c.info.has_video
                            else f"Áudio {c.info.audio_rate / 1000:g} kHz")
                    if c.info.has_video and not c.info.has_audio:
                        kind += " (sem áudio)"
                pos = ""
                if c.is_synced:
                    from ..media import seconds_to_tc
                    pos = seconds_to_tc(c.placement() - base, fps)
                conf = f"{c.confidence:.0f}" if c.status == Status.SYNCED else ""
                drift = (f"{c.drift * 1e6:+.1f} ppm" if c.drift_reliable and abs(c.drift) > 5e-7
                         else "")
                status = c.status + (f" — {c.message}" if c.message else "")
                failed = c.status in FAILED
                name = ("✖  " + c.name) if failed else c.name
                ci = QTreeWidgetItem([name, kind, fmt_dur(c.duration), pos, conf, drift, status])
                ci.setData(0, Qt.UserRole, ("clip", c.uid))
                ci.setToolTip(0, c.path)
                ci.setToolTip(6, status)
                ci.setFlags((ci.flags() | Qt.ItemIsDragEnabled) & ~Qt.ItemIsDropEnabled
                            & ~Qt.ItemIsEditable)
                ci.setForeground(6, QBrush(QColor(STATUS_COLORS.get(c.status, "#8E8E8E"))))
                if failed:   # linha inteira em vermelho
                    for col in range(len(COLS)):
                        ci.setForeground(col, QBrush(QColor(RED)))
                        ci.setBackground(col, QBrush(QColor(RED_BG)))
                ti.addChild(ci)
            ti.setExpanded(t.name not in expanded)
        self.tree.blockSignals(False)

        # combo de referência
        self.cb_ref.blockSignals(True)
        self.cb_ref.clear()
        self.cb_ref.addItem("Automática", None)
        for t in self.project.tracks:
            self.cb_ref.addItem(t.name, t.name)
        idx = self.cb_ref.findData(self.project.reference) if self.project.reference else 0
        self.cb_ref.setCurrentIndex(max(0, idx))
        self.cb_ref.blockSignals(False)

        self.timeline.set_project(self.project)
        n = len(self.project.all_clips())
        has_result = bool(synced)
        for a in (self.a_fcpxml, self.a_xml, self.a_replace, self.a_csv, self.a_json,
                  self.a_driftwav):
            a.setEnabled(has_result)
        n_fail = sum(1 for c in self.project.all_clips() if c.status in FAILED)
        self.status_label.setStyleSheet(f"color: {RED}; font-weight: bold;" if n_fail else "")
        if not self.busy():
            if n == 0:
                self.status_label.setText("Arraste arquivos ou pastas para começar.")
            elif n_fail:
                self.status_label.setText(
                    f"✖ {n_fail} clipe(s) NÃO sincronizado(s) (em vermelho) — "
                    f"{len(synced)} de {n} sincronizados.")
            elif has_result:
                self.status_label.setText(
                    f"{len(synced)} de {n} clipes sincronizados — use Exportar.")
            else:
                self.status_label.setText(
                    f"{n} arquivo(s) em {len(self.project.tracks)} faixa(s). Clique em Sincronizar.")

    # ---------------------------------------------------------------- adding
    def add_paths(self, paths: list[str], target_track: Optional[str] = None) -> None:
        if self.busy():
            return
        mode = "single" if target_track else self.cb_group.currentData()
        added = self.project.add_paths(paths, mode)
        if target_track:
            for c in added:
                self.project.move_clip(c, target_track)
            self.project.remove_empty_tracks()
        if not added:
            self.log("Nenhum arquivo de mídia novo encontrado.")
            return
        self.log(f"{len(added)} arquivo(s) adicionado(s).")
        self.set_last_dir(paths[0])
        self.refresh()
        self.probe_pending()

    def probe_pending(self) -> None:
        todo = [c for c in self.project.all_clips() if c.info is None]
        if not todo:
            return

        def work(progress, log, cancel):
            from concurrent.futures import ThreadPoolExecutor
            done = 0

            def one(c):
                try:
                    c.info = probe(c.path)
                except Exception as e:  # noqa: BLE001
                    c.status = Status.ERROR
                    c.message = str(e)
                    log(f"Erro ao ler {c.name}: {e}")
            with ThreadPoolExecutor(8) as ex:
                for _ in ex.map(one, todo):
                    done += 1
                    progress(done / len(todo), f"Lendo metadados ({done}/{len(todo)})")
                    if cancel.is_set():
                        break

        self.start_task(work, lambda _: self.refresh(), "Lendo metadados…")

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

    def on_files_dropped(self, paths: list, target) -> None:
        self.add_paths(paths, target)

    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:  # noqa: N802
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_paths(paths)

    # --------------------------------------------------------------- editing
    def on_clips_moved(self, uids: list, track: str) -> None:
        for uid in uids:
            c = self.project.clip_by_uid(uid)
            if c:
                self.project.move_clip(c, track)
        self.project.remove_empty_tracks()
        self.refresh()

    def on_new_track(self) -> None:
        name, ok = QInputDialog.getText(self, "Nova faixa", "Nome da faixa (câmera/gravador):",
                                        text=self.project.unique_track_name("Câmera"))
        if not ok or not name.strip():
            return
        t = self.project.add_track(name.strip())
        for c in self.selected_clips():
            self.project.move_clip(c, t.name)
        self.project.remove_empty_tracks() if self.selected_clips() else None
        self.refresh()

    def on_remove(self) -> None:
        clips = self.selected_clips()
        for name in self.selected_tracks():
            t = self.project.track(name)
            if t:
                clips += t.clips
        for c in set(clips):
            self.project.remove_clip(c)
        self.project.remove_empty_tracks()
        self.refresh()

    def on_clear(self) -> None:
        if self.project.all_clips() and QMessageBox.question(
                self, APP_NAME, "Remover todos os arquivos do projeto?") != QMessageBox.Yes:
            return
        self.project.clear()
        self.project_path = None
        self.refresh()

    def on_item_changed(self, item: QTreeWidgetItem, col: int) -> None:
        kind, val = item.data(0, Qt.UserRole)
        if kind == "track" and col == 0:
            new = self.project.rename_track(val, item.text(0))
            self.refresh()
            if new != val:
                self.log(f"Faixa renomeada: {val} → {new}")

    def on_double_click(self, item: QTreeWidgetItem, col: int) -> None:
        kind, val = item.data(0, Qt.UserRole)
        if kind == "clip":
            c = self.project.clip_by_uid(val)
            if c:
                QDesktopServices.openUrl(QUrl.fromLocalFile(c.path))
        elif col == 0:
            self.tree.editItem(item, 0)

    def on_ref_changed(self) -> None:
        self.project.reference = self.cb_ref.currentData()
        self.refresh()

    def on_tree_selection(self) -> None:
        self.timeline.canvas.set_selected({c.uid for c in self.selected_clips()})

    def select_clip(self, uid: int) -> None:
        for i in range(self.tree.topLevelItemCount()):
            ti = self.tree.topLevelItem(i)
            for k in range(ti.childCount()):
                ci = ti.child(k)
                if ci.data(0, Qt.UserRole) == ("clip", uid):
                    self.tree.setCurrentItem(ci)
                    self.tree.scrollToItem(ci)
                    return

    def on_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        kind, val = item.data(0, Qt.UserRole)
        m = QMenu(self)
        if kind == "track":
            m.addAction("★ Definir como referência", lambda: self._set_ref(val))
            m.addAction("Renomear", lambda: self.tree.editItem(item, 0))
            m.addSeparator()
            m.addAction("Remover faixa", self.on_remove)
        else:
            if not item.isSelected():
                self.tree.setCurrentItem(item)
            mv = m.addMenu("Mover para faixa")
            for t in self.project.tracks:
                mv.addAction(t.name, lambda n=t.name: self.on_clips_moved(
                    [c.uid for c in self.selected_clips()], n))
            mv.addSeparator()
            mv.addAction("Nova faixa…", self.on_new_track)
            c = self.project.clip_by_uid(val)
            m.addAction("Mostrar no Finder" if sys.platform == "darwin" else "Mostrar na pasta",
                        lambda: reveal_in_os(c.path))
            m.addSeparator()
            m.addAction("Remover", self.on_remove)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _set_ref(self, name: str) -> None:
        self.project.reference = name
        self.refresh()

    # ------------------------------------------------------------------ sync
    def on_sync(self) -> None:
        if self.busy():
            return
        if not self.project.all_clips():
            QMessageBox.information(self, APP_NAME, "Adicione arquivos primeiro.")
            return
        opts = SyncOptions(min_score=self.sp_score.value(),
                           compare_same_track=self.ck_same.isChecked(),
                           timeline_clock=self.cb_clock.currentData())
        self.log_view.appendPlainText("\n— Sincronização iniciada —")

        def work(progress, log, cancel):
            return SyncEngine(self.project, opts, progress=progress, log=log, cancel=cancel).run()

        def done(rep):
            self.refresh()
            msg = (f"{rep.synced} sincronizado(s), {rep.unsynced} não sincronizado(s)"
                   + (f", {rep.errors} erro(s)" if rep.errors else "")
                   + f" em {rep.elapsed:.1f} s.")
            if rep.unsynced or rep.errors:
                msg = "✖ " + msg + " Os que falharam estão em vermelho."
            self.status_label.setText(msg)
            if rep.warnings:
                QMessageBox.warning(self, APP_NAME, "\n\n".join(rep.warnings[:8]))
            elif rep.unsynced and rep.synced:
                self.log("Dica: clipes não sincronizados podem precisar de outra faixa, de menor "
                         "'Confiança mín.' ou não ter áudio em comum com os demais.")

        self.start_task(work, done, "Sincronizando…")

    def on_cancel(self) -> None:
        if self.task:
            self.task.cancel()
            self.status_label.setText("Cancelando…")

    # ---------------------------------------------------------------- export
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

    def _export_timeline(self, writer, path: str) -> None:
        fps = self.timeline_fps()
        name = Path(path).stem

        def write(corrected):
            tl = build_timeline(self.project, fps, name, corrected, self.ck_unsynced.isChecked())
            try:
                writer(tl, path)
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, APP_NAME, f"Falha ao exportar: {e}")
                return
            self.log(f"Exportado: {path}" + (f" ({len(corrected)} áudio(s) com drift corrigido)"
                                             if corrected else ""))
            self.status_label.setText(f"Exportado: {Path(path).name}")
            reveal_in_os(path)

        if self.ck_drift.isChecked() and drift_candidates(self.project):
            out_dir = str(Path(path).with_name(Path(path).stem + "_drift_corrigido"))
            self.start_task(lambda pr, lg, cn: export_drift_corrected(
                self.project, out_dir, progress=pr, cancel=cn, log=lg), write,
                "Corrigindo drift…")
        else:
            write({})

    def on_export_fcpxml(self) -> None:
        path = self._save_path("Exportar FCPXML", ".fcpxml", "FCPXML (*.fcpxml)")
        if path:
            ver = self.cb_fcpx.currentText()
            self._export_timeline(lambda tl, p: export_fcpxml(tl, p, version=ver), path)

    def on_export_xml(self) -> None:
        path = self._save_path("Exportar XML (Premiere)", ".xml", "XML (*.xml)")
        if path:
            self._export_timeline(export_xmeml, path)

    def on_export_csv(self) -> None:
        path = self._save_path("Exportar CSV", ".csv", "CSV (*.csv)")
        if path:
            export_csv(self.project, path, self.timeline_fps() or guess_fps(self.project))
            self.log(f"Exportado: {path}")

    def on_export_json(self) -> None:
        path = self._save_path("Exportar JSON", ".json", "JSON (*.json)")
        if path:
            export_json(self.project, path, self.timeline_fps() or guess_fps(self.project))
            self.log(f"Exportado: {path}")

    def on_export_replace(self) -> None:
        audio_tracks = [t.name for t in self.project.tracks
                        if any(c.is_synced and c.has_audio for c in t.clips)]
        if not audio_tracks:
            return
        ref = self.project.reference_track()
        dlg = ReplaceAudioDialog(audio_tracks, ref.name if ref else None, self)
        if dlg.exec() != QDialog.Accepted:
            return
        out = QFileDialog.getExistingDirectory(self, "Pasta de destino", self.last_dir())
        if not out:
            return
        master, keep = dlg.track.currentText(), dlg.keep.isChecked()

        def done(files):
            self.log(f"{len(files)} vídeo(s) exportado(s) em {out}")
            self.status_label.setText(f"{len(files)} vídeo(s) exportado(s).")
            if files:
                reveal_in_os(files[0])

        self.start_task(lambda pr, lg, cn: export_replaced_audio(
            self.project, master, out, keep, progress=pr, cancel=cn, log=lg), done,
            "Exportando vídeos…")

    def on_export_drift_only(self) -> None:
        cands = drift_candidates(self.project)
        if not cands:
            QMessageBox.information(self, APP_NAME, "Nenhum arquivo de áudio com drift relevante "
                                    "(> 2 ms ao longo do arquivo).")
            return
        out = QFileDialog.getExistingDirectory(self, "Pasta de destino", self.last_dir())
        if not out:
            return
        self.start_task(lambda pr, lg, cn: export_drift_corrected(
            self.project, out, progress=pr, cancel=cn, log=lg),
            lambda res: self.log(f"{len(res)} WAV(s) gerado(s) em {out}"), "Corrigindo drift…")

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
        self.refresh()

    def on_save_project(self) -> None:
        if not self.project_path:
            return self.on_save_project_as()
        self.project.save(self.project_path)
        self.status_label.setText(f"Projeto salvo: {Path(self.project_path).name}")

    def on_save_project_as(self) -> None:
        path = self._save_path("Salvar projeto", ".megasinc", "Projeto Mega Sinc (*.megasinc)")
        if path:
            self.project_path = path
            self.on_save_project()

    # ----------------------------------------------------------------- ajuda
    def on_help(self) -> None:
        QMessageBox.information(self, "Como usar", (
            "1. Arraste para a janela as pastas/arquivos de todas as câmeras e gravadores.\n"
            "   Cada aparelho vira uma faixa (corrija arrastando clipes entre faixas).\n\n"
            "2. (Opcional) Clique com o botão direito no gravador principal → "
            "'Definir como referência'.\n\n"
            "3. Clique em Sincronizar.\n\n"
            "4. Exportar:\n"
            "   • Premiere Pro: Exportar → XML → no Premiere, Arquivo → Importar.\n"
            "   • Final Cut Pro: Exportar → FCPXML → Arquivo → Importar → XML.\n"
            "   • DaVinci Resolve: qualquer um dos dois → File → Import → Timeline.\n"
            "   • Ou gere novos vídeos já com o áudio do gravador.\n\n"
            "Para gravações longas, ative 'Corrigir drift do áudio nos XMLs'."))

    def on_about(self) -> None:
        QMessageBox.about(self, APP_NAME, (
            f"<b>{APP_NAME} {__version__}</b><br><br>"
            "Sincronização automática de múltiplas câmeras e gravadores pelo áudio.<br><br>"
            "Correlação de envelope multibanda + GCC-PHAT com precisão de sub-amostra, "
            "detecção e correção de drift de clock.<br><br>"
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
        QMenuBar {{ background: #101010; }}
        QMenuBar::item:selected, QMenu::item:selected {{ background: {YELLOW}; color: #111; }}
        QMenu {{ background: #1c1c1c; border: 1px solid #333; }}
        QToolBar {{ background: #101010; border: none; border-bottom: 2px solid {YELLOW};
                    spacing: 4px; padding: 4px; }}
        QToolButton {{ color: #ECECEC; padding: 4px 8px; border-radius: 6px; }}
        QToolButton:hover {{ background: #2a2a2a; color: {YELLOW}; }}
        QToolButton:pressed {{ background: {YELLOW}; color: #111; }}
        QToolButton:disabled {{ color: #555555; }}
        QTreeWidget {{ border: 1px solid #2a2a2a; border-radius: 6px; }}
        QHeaderView::section {{ background: #1f1f1f; color: {YELLOW}; border: none;
                               border-right: 1px solid #2a2a2a; padding: 4px 6px; font-weight: bold; }}
        QTreeView::item:selected {{ background: {YELLOW}; color: #111; }}
        QComboBox, QDoubleSpinBox, QLineEdit {{ background: #1f1f1f; border: 1px solid #333;
                                               border-radius: 5px; padding: 3px 6px; }}
        QComboBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{ border: 1px solid {YELLOW}; }}
        QPushButton {{ background: #242424; border: 1px solid #333; border-radius: 5px;
                       padding: 4px 10px; }}
        QPushButton:hover {{ border: 1px solid {YELLOW}; color: {YELLOW}; }}
        QPushButton#syncButton {{ background: {YELLOW}; color: #111; font-weight: bold;
                                  font-size: 15px; border: none; border-radius: 8px; }}
        QPushButton#syncButton:hover {{ background: #FFD54A; color: #111; }}
        QPushButton#syncButton:disabled {{ background: #3a3a3a; color: #777; }}
        QCheckBox::indicator:checked {{ background: {YELLOW}; border: 1px solid {YELLOW};
                                       border-radius: 3px; }}
        QCheckBox::indicator:unchecked {{ background: #1f1f1f; border: 1px solid #444;
                                         border-radius: 3px; }}
        QProgressBar {{ background: #1f1f1f; border: 1px solid #333; border-radius: 5px;
                        text-align: center; color: #ECECEC; }}
        QProgressBar::chunk {{ background: {YELLOW}; border-radius: 4px; }}
        QPlainTextEdit {{ background: #0e0e0e; border: 1px solid #2a2a2a; border-radius: 6px;
                          color: #BDBDBD; }}
        QStatusBar {{ background: #101010; color: #BDBDBD; }}
        QSplitter::handle {{ background: #1f1f1f; }}
        QSplitter::handle:hover {{ background: {YELLOW}; }}
        QScrollBar:horizontal, QScrollBar:vertical {{ background: #141414; border: none; }}
        QScrollBar::handle {{ background: #3a3a3a; border-radius: 4px; min-height: 20px; min-width: 20px; }}
        QScrollBar::handle:hover {{ background: {YELLOW}; }}
    """)
