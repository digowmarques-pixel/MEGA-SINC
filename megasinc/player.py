"""Reprodução da timeline sincronizada para conferir a sincronia de ouvido.

Mistura o áudio de análise (16 kHz mono, já em cache) de todas as faixas ativas
na posição da timeline e envia para a saída de som com `sounddevice`. Se dois
microfones estiverem fora de sincronia, ouve-se eco/"flam" — que some quando a
sincronia está certa.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .media import ANALYSIS_SR


@dataclass
class Source:
    track: str
    start: float          # segundos na timeline
    pcm: np.ndarray       # int16, ANALYSIS_SR, mono

    @property
    def end(self) -> float:
        return self.start + len(self.pcm) / ANALYSIS_SR


def render(sources: list[Source], t0: float, n: int, out_sr: int,
           active: Optional[set[str]] = None) -> np.ndarray:
    """Mistura `n` amostras (taxa `out_sr`) a partir de `t0`. Retorna float32 (n, 2)."""
    dur = n / out_sr
    m = int(np.ceil(dur * ANALYSIS_SR)) + 2
    buf = np.zeros(m, dtype=np.float32)
    k = 0
    base = int(np.floor(t0 * ANALYSIS_SR))
    frac = t0 * ANALYSIS_SR - base
    for s in sources:
        if active is not None and s.track not in active:
            continue
        if s.end <= t0 or s.start >= t0 + dur:
            continue
        i0 = base - int(round(s.start * ANALYSIS_SR))
        a, b = max(0, i0), min(len(s.pcm), i0 + m)
        if b <= a:
            continue
        buf[a - i0:b - i0] += np.asarray(s.pcm[a:b], dtype=np.float32) * (1.0 / 32768.0)
        k += 1
    if k > 1:
        buf *= 1.0 / np.sqrt(k)
    x = frac + np.arange(n) * (ANALYSIS_SR / out_sr)
    out = np.interp(x, np.arange(m), buf).astype(np.float32)
    np.clip(out, -1.0, 1.0, out=out)
    return np.repeat(out[:, None], 2, axis=1)


class Player:
    """Toca a mistura em tempo real. Thread-safe para seek/pause a partir da interface."""

    def __init__(self) -> None:
        self.sources: list[Source] = []
        self.active: Optional[set[str]] = None
        self.position = 0.0
        self.end = 0.0
        self.playing = False
        self._stream = None
        self._lock = threading.Lock()
        self.on_stop: Optional[Callable[[], None]] = None

    @staticmethod
    def available() -> tuple[bool, str]:
        try:
            import sounddevice as sd
            sd.query_devices(kind="output")
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, str(e) or "sem saída de áudio"

    def load(self, sources: list[Source]) -> None:
        with self._lock:
            self.sources = sources
            self.end = max((s.end for s in sources), default=0.0)

    def seek(self, t: float) -> None:
        with self._lock:
            self.position = max(0.0, min(t, self.end))

    def set_active(self, active: Optional[set[str]]) -> None:
        with self._lock:
            self.active = active

    def play(self) -> None:
        import sounddevice as sd
        if self.playing:
            return
        dev = sd.query_devices(kind="output")
        sr = int(dev.get("default_samplerate") or 48000)

        def cb(outdata, frames, _time, _status):
            with self._lock:
                t = self.position
                data = render(self.sources, t, frames, sr, self.active)
                self.position = t + frames / sr
                done = self.position >= self.end
            outdata[:] = data
            if done:
                raise sd.CallbackStop()

        self._stream = sd.OutputStream(samplerate=sr, channels=2, dtype="float32",
                                       callback=cb, blocksize=1024,
                                       finished_callback=self._finished)
        self.playing = True
        self._stream.start()

    def _finished(self) -> None:
        self.playing = False
        if self.on_stop:
            self.on_stop()

    def pause(self) -> None:
        st, self._stream = self._stream, None
        self.playing = False
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:  # noqa: BLE001
                pass
