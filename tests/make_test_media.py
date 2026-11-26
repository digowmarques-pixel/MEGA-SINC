"""Gera mídia de teste sintética com defasagens conhecidas.

Cenário:
  * Gravador (referência): REC_0001.wav [0, 100) e REC_0002.wav [101.5, 200)
  * Câmera A (29,97 fps): CAMA_0001.mov [10, 70) e CAMA_0002.mov [80.123, 150)
  * Câmera B (25 fps, clock com drift de +60 ppm): CAMB_0001.mov [5.555, 190)
  * Arquivo sem relação: OUTRO_0001.wav (deve ficar "não sincronizado")
Os sinais das câmeras passam por filtro, ganho e ruído diferentes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, resample_poly, sosfiltfilt

SR = 48000
TOTAL = 200.0
RNG = np.random.default_rng(1234)


def speechlike(duration: float, rng: np.random.Generator) -> np.ndarray:
    n = int(duration * SR)
    out = np.zeros(n, np.float32)
    t = 0.0
    while t < duration:
        seg = rng.uniform(0.05, 0.45)
        if rng.random() < 0.25:
            t += rng.uniform(0.2, 1.2)  # pausa
            continue
        i0, i1 = int(t * SR), min(n, int((t + seg) * SR))
        m = i1 - i0
        if m <= 10:
            break
        f0 = rng.uniform(90, 260)
        tt = np.arange(m) / SR
        voiced = sum(np.sin(2 * np.pi * f0 * h * tt + rng.uniform(0, 6)) / h for h in range(1, 12))
        noise = rng.standard_normal(m)
        lo, hi = sorted(rng.uniform(200, 6000, 2))
        sos = butter(2, [lo, max(hi, lo + 300)], btype="band", fs=SR, output="sos")
        sig = 0.6 * voiced + sosfiltfilt(sos, noise) * rng.uniform(0.2, 1.5)
        env = np.sin(np.pi * np.arange(m) / m) ** rng.uniform(0.5, 2)
        out[i0:i1] += (sig * env * rng.uniform(0.05, 0.3)).astype(np.float32)
        t += seg
    return out


def write_wav(path: Path, x: np.ndarray, sr: int = SR, channels: int = 1) -> None:
    x = np.clip(x, -1, 1)
    data = (x * 32767).astype("<i2")
    if channels == 2:
        data = np.repeat(data[:, None], 2, axis=1).reshape(-1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())


def camera_audio(master: np.ndarray, start: float, dur: float, rng, lp: float, gain: float,
                 noise: float) -> np.ndarray:
    seg = master[int(round(start * SR)): int(round((start + dur) * SR))].copy()
    sos = butter(4, lp, btype="low", fs=SR, output="sos")
    seg = sosfiltfilt(sos, seg) * gain
    seg += rng.standard_normal(len(seg)).astype(np.float32) * noise
    return seg.astype(np.float32)


def make_video(path: Path, wav: Path, fps: str, dur: float, tc: str | None = None) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
           f"testsrc2=size=320x180:rate={fps}:duration={dur}", "-i", str(wav),
           "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    if tc:
        cmd += ["-timecode", tc]
    subprocess.run(cmd + [str(path)], check=True)


def main(out_dir: str) -> dict:
    out = Path(out_dir)
    for sub in ("recorder", "camA", "camB", "other"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    master = speechlike(TOTAL, rng)
    master += rng.standard_normal(len(master)).astype(np.float32) * 0.002
    truth = {}

    # gravador (estéreo, 2 arquivos)
    for name, s, e in (("REC_0001.wav", 0.0, 100.0), ("REC_0002.wav", 101.5, 200.0)):
        write_wav(out / "recorder" / name, master[int(s * SR): int(e * SR)], channels=2)
        truth[f"recorder/{name}"] = s

    # câmera A
    for name, s, d in (("CAMA_0001", 10.0, 60.0), ("CAMA_0002", 80.123, 69.877)):
        a = camera_audio(master, s, d, rng, lp=5000, gain=0.5, noise=0.01)
        w = out / "camA" / f"{name}.wav"
        write_wav(w, a)
        make_video(out / "camA" / f"{name}.mov", w, "30000/1001", d, tc="01:00:00:00")
        w.unlink()
        truth[f"camA/{name}.mov"] = s

    # câmera B com drift: o clock da câmera roda 60 ppm mais rápido
    s, d = 5.555, 184.0
    seg = master[int(s * SR): int((s + d * 1.0001) * SR)]
    up, down = 50003, 50000  # +60 ppm
    seg = resample_poly(seg.astype(np.float64), up, down)[: int(d * SR)].astype(np.float32)
    sos = butter(2, [150, 7000], btype="band", fs=SR, output="sos")
    seg = (sosfiltfilt(sos, seg) * 1.8 + rng.standard_normal(len(seg)) * 0.004).astype(np.float32)
    w = out / "camB" / "CAMB_0001.wav"
    write_wav(w, seg)
    make_video(out / "camB" / "CAMB_0001.mov", w, "25", d)
    w.unlink()
    truth["camB/CAMB_0001.mov"] = s

    # arquivo sem relação
    write_wav(out / "other" / "OUTRO_0001.wav", speechlike(40.0, np.random.default_rng(7)))
    truth["other/OUTRO_0001.wav"] = None

    (out / "truth.json").write_text(json.dumps(truth, indent=2))
    return truth


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1] if len(sys.argv) > 1 else "test_media"), indent=2))
