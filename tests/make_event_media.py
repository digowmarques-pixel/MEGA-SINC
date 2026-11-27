"""Cenário realista "duas câmeras, muitos clipes curtos, sem gravador".

Evento de `total` s com música repetitiva (batida + loop de acordes), fala e
público. Câmera P (13 clipes) e câmera Q (10 clipes) gravam trechos aleatórios
com intervalos; só parte dos clipes se sobrepõe à outra câmera. Os relógios das
câmeras diferem (+37,4 s) e o horário fica no metadado creation_time (precisão
de 1 s), como numa Lumix.
"""
from __future__ import annotations

import json
import subprocess
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfiltfilt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_test_media import speechlike  # noqa: E402

SR = 48000
EPOCH = 1795000000.0


def music(total: float, rng) -> np.ndarray:
    n = int(total * SR)
    t = np.arange(n) / SR
    out = np.zeros(n, np.float32)
    beat = 0.48
    kick = np.sin(2 * np.pi * 60 * np.arange(int(0.12 * SR)) / SR) * \
        np.exp(-np.arange(int(0.12 * SR)) / (0.03 * SR))
    for k in range(int(total / beat)):
        i = int(k * beat * SR)
        out[i:i + len(kick)] += 0.35 * kick[: n - i]
    chords = [(220, 277, 330), (196, 247, 294), (175, 220, 262), (196, 247, 294)]
    bar = beat * 4
    for k in range(int(total / bar) + 1):
        f = chords[k % 4]
        i0, i1 = int(k * bar * SR), min(n, int((k + 1) * bar * SR))
        tt = t[i0:i1]
        out[i0:i1] += 0.05 * sum(np.sin(2 * np.pi * fr * tt) for fr in f).astype(np.float32)
    return out


def write_wav(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def schedule(total: float, n: int, rng, near=None) -> list[tuple[float, float]]:
    """Clipes em ordem, sem sobreposição. Com `near`, a maioria começa perto dos
    clipes da outra câmera (dois operadores filmando a mesma cena)."""
    out, t = [], rng.uniform(2, 20)
    anchors = list(near or [])
    for _ in range(n):
        d = float(rng.uniform(8, 70))
        if anchors and rng.random() < 0.75:
            a = anchors.pop(0)
            t = max(t, a[0] + rng.uniform(-20, a[1] * 0.8))
        if t + d > total - 1:
            break
        out.append((round(t, 3), round(d, 3)))
        t += d + rng.uniform(4, 40)
        while anchors and anchors[0][0] + anchors[0][1] < t:
            anchors.pop(0)
    return out


def main(out_dir: str, total: float = 1200.0, seed: int = 11, video: bool = True) -> dict:
    rng = np.random.default_rng(seed)
    out = Path(out_dir)
    ev = music(total, rng) + speechlike(total, rng) * 0.8
    ev += rng.standard_normal(len(ev)).astype(np.float32) * 0.01      # público
    truth = {}
    sched_p = schedule(total, 13, rng)
    sched_q = schedule(total, 10, rng, near=sched_p)
    for cam, prefix, sched, clock, fps, lp in (("P", "P1458", sched_p, 0.0, "30000/1001", 6000),
                                               ("Q", "P1018", sched_q, 37.4, "24000/1001", 4000)):
        d = out / cam
        d.mkdir(parents=True, exist_ok=True)
        for k, (s, dur) in enumerate(sched):
            name = f"{prefix}{100 + k:03d}"
            seg = ev[int(s * SR): int((s + dur) * SR)].astype(np.float64)
            sos = butter(2, [120, lp], btype="band", fs=SR, output="sos")
            seg = sosfiltfilt(sos, seg) * rng.uniform(0.4, 1.2)
            seg += rng.standard_normal(len(seg)) * 0.02
            wav = d / f"{name}.wav"
            write_wav(wav, seg.astype(np.float32))
            created = datetime.fromtimestamp(int(EPOCH + clock + s), tz=timezone.utc)
            vin = ["-f", "lavfi", "-i", f"testsrc2=size=160x90:rate={fps}:duration={dur}"] \
                if video else []
            vmap = ["-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast"] \
                if video else ["-map", "0:a"]
            subprocess.run(["ffmpeg", "-v", "error", "-y", *vin, "-i", str(wav), *vmap,
                            "-c:a", "aac", "-b:a", "128k", "-shortest",
                            "-metadata", f"creation_time={created.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                            str(d / f"{name}.MOV")], check=True)
            wav.unlink()
            truth[f"{cam}/{name}.MOV"] = {"start": s, "dur": dur}
    (out / "truth.json").write_text(json.dumps(truth, indent=1))
    return truth


if __name__ == "__main__":
    main(sys.argv[1])
