"""Execução de tarefas longas fora da thread da interface."""
from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from ..media import Cancelled


class TaskThread(QThread):
    """Roda fn(progress, log, cancel) em segundo plano.

    progress(frac: float, msg: str) e log(msg: str) podem ser chamados da thread de
    trabalho; os sinais chegam na thread da interface."""

    progressed = Signal(float, str)
    logged = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, fn: Callable[..., Any], parent=None) -> None:
        super().__init__(parent)
        self.fn = fn
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            result = self.fn(self.progressed.emit, self.logged.emit, self.cancel_event)
        except Cancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001
            self.logged.emit(traceback.format_exc())
            self.failed.emit(str(e) or e.__class__.__name__)
        else:
            if self.cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)
