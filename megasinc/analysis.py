"""Análise de áudio: features de onset, correlação grosseira e refinamento fino.

Estratégia (em duas etapas, como os sincronizadores comerciais):

1. **Grosseira** — cada arquivo vira um "envelope de ataques" multibanda a 100 Hz
   (diferença positiva da energia logarítmica em 5 bandas). Isso é robusto a
   microfones diferentes, EQ, ganho e ruído. A correlação cruzada via FFT entre
   dois envelopes encontra o deslocamento com resolução de ~10 ms, mesmo em
   arquivos de horas.
2. **Fina** — em várias janelas ao longo da sobreposição, GCC-PHAT sobre o áudio
   bruto a 16 kHz dá a defasagem com precisão de sub-amostra (< 0,1 ms). Uma
   regressão linear das defasagens locais mede o *drift* de clock entre os
   gravadores (ppm).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from .media import ANALYSIS_SR


def next_fast_len(n: int) -> int:
    """Menor inteiro >= n da forma 2^a * 3^b * 5^c (tamanho eficiente para FFT)."""
    if n <= 6:
        return max(1, n)
    best = 1 << (n - 1).bit_length()
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            q = -(-n // p35)                        # ceil(n / p35)
            p2 = 1 << (q - 1).bit_length() if q > 1 else 1
            cand = p2 * p35
            if cand < best:
                best = cand
            p35 *= 3
        p5 *= 5
    return best


def _moving_average(x: np.ndarray, size: int) -> np.ndarray:
    """Média móvel centrada ao longo do eixo 0, bordas replicadas."""
    h = size // 2
    pad = np.concatenate([np.repeat(x[:1], h, axis=0), x, np.repeat(x[-1:], h, axis=0)])
    cs = np.cumsum(pad, axis=0, dtype=np.float64)
    cs = np.concatenate([np.zeros((1,) + x.shape[1:]), cs])
    return ((cs[size:] - cs[:-size]) / size).astype(x.dtype)

FRAME_RATE = 100                      # features por segundo
HOP = ANALYSIS_SR // FRAME_RATE       # 160 amostras
NFFT = 512
BAND_EDGES = [80, 300, 800, 2000, 4500, 7900]
N_BANDS = len(BAND_EDGES) - 1


def _band_matrix() -> np.ndarray:
    freqs = np.fft.rfftfreq(NFFT, 1.0 / ANALYSIS_SR)
    m = np.zeros((len(freqs), N_BANDS), dtype=np.float32)
    for i in range(N_BANDS):
        m[(freqs >= BAND_EDGES[i]) & (freqs < BAND_EDGES[i + 1]), i] = 1.0
    return m


_BANDS = _band_matrix()
_WIN = np.hanning(NFFT).astype(np.float32)


def compute_features(pcm: np.ndarray, block_frames: int = 30000) -> np.ndarray:
    """Retorna matriz (N_BANDS, n_frames) float32 com o envelope de ataques normalizado."""
    n = len(pcm)
    if n < NFFT:
        return np.zeros((N_BANDS, 0), dtype=np.float32)
    n_frames = 1 + (n - NFFT) // HOP
    loge = np.empty((n_frames, N_BANDS), dtype=np.float32)
    for f0 in range(0, n_frames, block_frames):
        f1 = min(n_frames, f0 + block_frames)
        s0, s1 = f0 * HOP, (f1 - 1) * HOP + NFFT
        x = np.asarray(pcm[s0:s1], dtype=np.float32) * (1.0 / 32768.0)
        frames = sliding_window_view(x, NFFT)[::HOP][: f1 - f0]
        spec = np.abs(np.fft.rfft(frames * _WIN, axis=1)) ** 2
        loge[f0:f1] = 10.0 * np.log10(spec.astype(np.float32) @ _BANDS + 1e-12)
    return postprocess_features(loge)


def postprocess_features(loge: np.ndarray) -> np.ndarray:
    # Piso de ruído: abaixo do percentil 20 de cada banda vira constante (sem ataques).
    floor = np.percentile(loge, 20, axis=0)
    loge = np.maximum(loge, floor[None, :] + 3.0)
    d = np.diff(loge, axis=0, prepend=loge[:1])
    o = np.maximum(d, 0.0)
    # Remove a média local (~1 s): trechos de silêncio ficam em zero e não
    # "atraem" a correlação para sobreposições maiores.
    o = o - _moving_average(o, FRAME_RATE + 1)
    std = o.std(axis=0)
    std[std < 1e-6] = 1.0
    o = np.clip(o / std[None, :], -8.0, 8.0)
    return np.ascontiguousarray(o.T, dtype=np.float32)


# --------------------------------------------------------------------------
# Etapa 1: correlação grosseira
# --------------------------------------------------------------------------

@dataclass
class CoarseMatch:
    lag: float        # segundos: tempo de A onde começa B  (tau_A = tau_B + lag)
    score: float      # destaque do pico (desvios robustos acima da mediana)
    ratio: float      # pico principal / segundo pico (ambiguidade)
    overlap: float    # segundos de sobreposição no pico


def coarse_match(fa: np.ndarray, fb: np.ndarray, min_overlap_s: float = 3.0,
                 lag_window: Optional[tuple[float, float]] = None) -> Optional[CoarseMatch]:
    """`lag_window=(min, max)` em segundos restringe a busca (busca guiada)."""
    nb_a, na = fa.shape
    nbb = fb.shape[1]
    if na < 20 or nbb < 20:
        return None
    n = next_fast_len(na + nbb - 1)
    spec = np.fft.rfft(fa, n, axis=1) * np.conj(np.fft.rfft(fb, n, axis=1))
    c = np.fft.irfft(spec.sum(axis=0), n)
    cc = np.concatenate([c[n - (nbb - 1):], c[:na]])
    lags = np.arange(-(nbb - 1), na)
    overlap = np.minimum(na, lags + nbb) - np.maximum(0, lags)
    min_ov = max(10, min(int(min_overlap_s * FRAME_RATE), int(0.8 * min(na, nbb))))
    valid = overlap >= min_ov
    if lag_window is not None:
        lo, hi = lag_window
        valid &= (lags >= lo * FRAME_RATE) & (lags <= hi * FRAME_RATE)
    if valid.sum() < 10:
        return None
    z = np.full(cc.shape, -np.inf)
    z[valid] = cc[valid] / np.sqrt(overlap[valid] * nb_a)
    zv = z[valid]
    med = float(np.median(zv))
    mad = float(np.median(np.abs(zv - med))) * 1.4826 + 1e-9
    k = int(np.argmax(z))
    peak = float(z[k])
    excl = int(0.3 * FRAME_RATE)
    z2 = z.copy()
    z2[max(0, k - excl): k + excl + 1] = -np.inf
    second = float(np.max(z2)) if np.isfinite(np.max(z2)) else med
    score = (peak - med) / mad
    ratio = (peak - med) / max(second - med, 1e-9)
    delta = 0.0
    if 0 < k < len(z) - 1 and np.isfinite(z[k - 1]) and np.isfinite(z[k + 1]):
        y0, y1, y2 = z[k - 1], z[k], z[k + 1]
        den = y0 - 2 * y1 + y2
        if den < 0:
            delta = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
    return CoarseMatch(lag=(lags[k] + delta) / FRAME_RATE, score=score, ratio=ratio,
                       overlap=float(overlap[k]) / FRAME_RATE)


# --------------------------------------------------------------------------
# Etapa 2: refinamento com GCC-PHAT e medição de drift
# --------------------------------------------------------------------------

@dataclass
class FineMatch:
    lag: float          # defasagem em t_ref (tau_A = tau_B + lag)
    t_ref: float        # tempo de B onde `lag` vale
    drift: float        # e: tau_A = L0 + (1+e) * tau_B
    drift_ok: bool      # drift medido de forma confiável
    drift_err: float
    windows: int
    quality: float


def _gcc_phat(xa: np.ndarray, xb: np.ndarray, beta: float = 0.8) -> np.ndarray:
    n = next_fast_len(len(xa) + len(xb))
    x = np.fft.rfft(xa, n) * np.conj(np.fft.rfft(xb, n))
    mag = np.abs(x)
    x /= mag ** beta + 1e-9 * (mag.max() + 1e-30)
    return np.fft.irfft(x, n)


MAX_DRIFT = 0.002   # 2000 ppm: acima disso não é drift de clock


def _theil_sen(t: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Reta robusta y = a + b t. Retorna (b, a)."""
    if len(t) < 2:
        return 0.0, float(np.median(y))
    i, j = np.triu_indices(len(t), 1)
    dt = t[j] - t[i]
    ok = dt > 1e-6
    slope = float(np.median((y[j] - y[i])[ok] / dt[ok])) if ok.any() else 0.0
    if abs(slope) > MAX_DRIFT:
        slope = 0.0
    return slope, float(np.median(y - slope * t))


def _peak(r: np.ndarray, lo: int = 0, hi: Optional[int] = None) -> tuple[int, float, float]:
    """Pico de r[lo:hi] -> (índice, ajuste sub-amostra, pico/rms)."""
    hi = len(r) if hi is None else hi
    seg = r[lo:hi]
    k = int(np.argmax(seg))
    q = float(seg[k]) / (float(np.sqrt(np.mean(r ** 2))) + 1e-12)
    d = 0.0
    if 0 < k < len(seg) - 1:
        y0, y1, y2 = seg[k - 1], seg[k], seg[k + 1]
        den = y0 - 2 * y1 + y2
        if den < 0:
            d = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
    elif len(seg) > 1:
        q = 0.0      # pico na borda da busca: não confiável
    return lo + k, d, q


def refine_match(pa: np.ndarray, pb: np.ndarray, coarse_lag: float,
                 sr: int = ANALYSIS_SR, win_s: float = 8.0, search_s: float = 0.35,
                 max_windows: int = 48, block_s: float = 90.0) -> Optional[FineMatch]:
    """Refina `coarse_lag` com GCC-PHAT.

    1. Calcula a GCC-PHAT em até `max_windows` janelas ao longo da sobreposição.
    2. Soma as correlações de janelas vizinhas (blocos de ~`block_s` s): o pico
       verdadeiro se reforça e o ruído se cancela — funciona mesmo com SNR baixo
       e muita reverberação.
    3. Ajusta uma reta robusta (defasagem x tempo) aos blocos -> drift inicial.
    4. Mede cada janela numa busca estreita (±2,5 ms) em torno da reta e ajusta
       a reta final por mínimos quadrados ponderados.
    """
    L = int(round(coarse_lag * sr))
    t0 = max(0.0, coarse_lag)
    t1 = min(len(pa) / sr, coarse_lag + len(pb) / sr)
    ov = t1 - t0
    if ov < 1.0:
        return None
    w = min(win_s, ov * 0.6)
    s = min(search_s, max(0.05, ov * 0.15))
    W, S = int(round(w * sr)), int(round(s * sr))
    b0_min = max(0, S - L)
    b0_max = min(len(pb) - W, len(pa) - L - W - S)
    if b0_max < b0_min or W < sr // 4:
        return None
    nwin = int(np.clip((b0_max - b0_min) / sr / (w * 1.2) + 1, 1, max_windows))
    rs, tcs = [], []
    for b0 in np.linspace(b0_min, b0_max, nwin).round().astype(np.int64):
        a0 = int(b0 + L - S)
        xa = np.asarray(pa[a0:a0 + W + 2 * S], dtype=np.float32)
        xb = np.asarray(pb[b0:b0 + W], dtype=np.float32)
        if np.sqrt(np.mean(xa ** 2)) < 3.0 or np.sqrt(np.mean(xb ** 2)) < 3.0:
            continue      # silêncio digital
        r = _gcc_phat(xa - xa.mean(), xb - xb.mean())[: 2 * S + 1]
        rms = float(np.sqrt(np.mean(r ** 2)))
        if rms <= 0:
            continue
        rs.append(r / rms)
        tcs.append((b0 + W / 2) / sr)
    if not rs:
        return None
    R = np.array(rs)
    tc = np.array(tcs)

    def to_lag(k: float) -> float:          # índice em r -> defasagem (s)
        return (L - S + k) / sr

    def aggregate(members: np.ndarray, slope: float) -> np.ndarray:
        tm = tc[members].mean()
        acc = np.zeros(R.shape[1])
        for m in members:
            acc += np.roll(R[m], -int(round(slope * (tc[m] - tm) * sr)))
        return acc

    def block_points(slope: float):
        ids = np.floor((tc - tc[0]) / block_s).astype(int)
        pts = []
        for b in np.unique(ids):
            mem = np.where(ids == b)[0]
            k, d, q = _peak(aggregate(mem, slope))
            if q >= 5.0:
                pts.append((tc[mem].mean(), to_lag(k + d), q))
        return np.array(pts).reshape(-1, 3)

    blocks = block_points(0.0)
    if len(blocks) == 0:
        k, d, q = _peak(aggregate(np.arange(len(tc)), 0.0))
        if q < 5.0:
            return None
        blocks = np.array([[tc.mean(), to_lag(k + d), q]])
    slope, icpt = _theil_sen(blocks[:, 0], blocks[:, 1])
    if len(blocks) >= 3 and abs(slope) > 0:
        blocks = block_points(slope)            # 2ª passada compensando o drift
        if len(blocks):
            slope, icpt = _theil_sen(blocks[:, 0], blocks[:, 1])
    keep = np.abs(blocks[:, 1] - (icpt + slope * blocks[:, 0])) < 0.005
    blocks = blocks[keep] if keep.any() else blocks

    # medição fina por janela em torno da reta
    half = int(round(0.0025 * sr))
    pts = []
    for r, t in zip(R, tc):
        kc = int(round((icpt + slope * t) * sr - (L - S)))
        lo, hi = kc - half, kc + half + 1
        if lo < 1 or hi > len(r) - 1:
            continue
        k, d, q = _peak(r, lo, hi)
        if q >= 4.0:
            pts.append((t, to_lag(k + d), q))
    good = np.array(pts).reshape(-1, 3)

    t_mid = (t0 + t1) / 2 - coarse_lag
    drift, drift_err, drift_ok = 0.0, float("inf"), False
    if len(good) >= 3 and np.ptp(good[:, 0]) >= 20.0:
        tb, lg, q = good[:, 0], good[:, 1], np.minimum(good[:, 2], 50.0)
        for _ in range(4):
            A = np.vstack([np.ones_like(tb), tb]).T
            sw = np.sqrt(q)[:, None]
            coef, *_ = np.linalg.lstsq(A * sw, lg * sw[:, 0], rcond=None)
            res = lg - A @ coef
            keep = np.abs(res) < max(0.0005, 3.5 * np.median(np.abs(res)) + 2e-5)
            if keep.all() or keep.sum() < 3:
                break
            tb, lg, q = tb[keep], lg[keep], q[keep]
        A = np.vstack([np.ones_like(tb), tb]).T
        coef, *_ = np.linalg.lstsq(A * np.sqrt(q)[:, None], lg * np.sqrt(q), rcond=None)
        res = lg - A @ coef
        sxx = float(np.sum((tb - tb.mean()) ** 2)) + 1e-12
        drift_err = float(np.sqrt(np.sum(res ** 2) / max(1, len(tb) - 2) / sxx))
        drift = float(coef[1])
        drift_ok = drift_err < max(3e-6, 0.3 * abs(drift)) and abs(drift) < MAX_DRIFT
        if drift_ok:
            return FineMatch(float(coef[0] + coef[1] * t_mid), t_mid, drift, True, drift_err,
                             int(len(tb)), float(np.median(q)))
        return FineMatch(float(np.average(lg, weights=q)), float(np.average(tb, weights=q)),
                         0.0, False, drift_err, int(len(tb)), float(np.median(q)))
    if len(good) >= 1:
        q = np.minimum(good[:, 2], 50.0)
        return FineMatch(float(np.average(good[:, 1], weights=q)),
                         float(np.average(good[:, 0], weights=q)), 0.0, False, drift_err,
                         int(len(good)), float(np.median(q)))
    # só os blocos agregados (sinal muito fraco por janela)
    if len(blocks) >= 3 and np.ptp(blocks[:, 0]) >= 60.0:
        res = blocks[:, 1] - (icpt + slope * blocks[:, 0])
        sxx = float(np.sum((blocks[:, 0] - blocks[:, 0].mean()) ** 2)) + 1e-12
        drift_err = float(np.sqrt(np.sum(res ** 2) / max(1, len(blocks) - 2) / sxx))
        drift_ok = drift_err < max(3e-6, 0.3 * abs(slope))
    if drift_ok:
        return FineMatch(icpt + slope * t_mid, t_mid, slope, True, drift_err, len(blocks),
                         float(np.median(blocks[:, 2])))
    t_ref = float(blocks[:, 0].mean())
    return FineMatch(icpt + slope * t_ref, t_ref, 0.0, False, drift_err, len(blocks),
                     float(np.median(blocks[:, 2])))
