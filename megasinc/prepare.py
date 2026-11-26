"""Preparação de um clipe: extrai o áudio de análise, calcula as features de
sincronia e a forma de onda para exibição. Tudo fica em cache em disco, então a
preparação feita ao adicionar os arquivos deixa o "Sincronizar" quase instantâneo."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .analysis import compute_features
from .media import ANALYSIS_SR, cache_key, extract_pcm, open_pcm, probe
from .project import Clip

WAVE_RATE = 100   # pontos de forma de onda por segundo


def compute_waveform(pcm: np.ndarray, rate: int = WAVE_RATE) -> np.ndarray:
    """Pico absoluto por bloco, normalizado para 0..1 (float16)."""
    block = ANALYSIS_SR // rate
    n = len(pcm) // block
    if n == 0:
        return np.zeros(0, dtype=np.float16)
    out = np.empty(n, dtype=np.float32)
    step = 360_000          # processa em pedaços para não carregar tudo
    for i in range(0, n, step):
        j = min(n, i + step)
        x = np.asarray(pcm[i * block: j * block], dtype=np.float32).reshape(j - i, block)
        out[i:j] = np.abs(x).max(axis=1)
    ref = float(np.percentile(out, 99.5)) if n > 10 else float(out.max())
    if ref <= 0:
        ref = 1.0
    return np.clip(out / ref, 0, 1).astype(np.float16)


def prepare_clip(clip: Clip, cache_dir: Path, cancel: Optional[threading.Event] = None,
                 progress: Optional[Callable[[float], None]] = None):
    """Garante metadados, PCM, features e forma de onda do clipe.
    Retorna (pcm, features) — ou (None, None) se o arquivo não tem áudio."""
    if clip.info is None:
        clip.info = probe(clip.path)
    if not clip.info.has_audio:
        clip.prep = 1.0
        return None, None
    key = cache_key(clip.path)
    pcm_path = cache_dir / f"{key}.pcm"
    feat_path = cache_dir / f"{key}.feat.npy"
    wave_path = cache_dir / f"{key}.wave.npy"

    def rep(f: float) -> None:
        clip.prep = 0.9 * f
        if progress:
            progress(clip.prep)

    if not pcm_path.exists():
        extract_pcm(clip.info, pcm_path, cancel=cancel, progress=rep)
    else:
        os.utime(pcm_path)
    pcm = open_pcm(pcm_path)
    if feat_path.exists():
        feat = np.load(feat_path)
    else:
        feat = compute_features(pcm)
        np.save(feat_path, feat)
    if clip.wave is None:
        if wave_path.exists():
            clip.wave = np.load(wave_path)
        else:
            clip.wave = compute_waveform(pcm)
            np.save(wave_path, clip.wave)
    if len(pcm) and not clip.info.has_video:
        clip.info.duration = len(pcm) / ANALYSIS_SR   # duração exata
    clip.prep = 1.0
    if progress:
        progress(1.0)
    return pcm, feat
