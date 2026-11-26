"""Exportação de mídia: áudio substituído e correção de drift (via FFmpeg)."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ..media import _POPEN_KW, Cancelled, FFmpegError, probe, require_ffmpeg
from ..project import Clip, Project
from ..timeline import Corrected

ProgressFn = Callable[[float, str], None]


def _run_ffmpeg(cmd: list[str], cancel: Optional[threading.Event]) -> None:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, **_POPEN_KW)
    err: list[bytes] = []
    t = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    t.start()
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            proc.kill()
            proc.wait()
            raise Cancelled()
        try:
            proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
    t.join(timeout=5)
    if proc.returncode != 0:
        msg = b"".join(err).decode("utf-8", "replace").strip()
        raise FFmpegError(msg.splitlines()[-1] if msg else "ffmpeg falhou")


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    k = 2
    while True:
        cand = path.with_name(f"{path.stem}_{k}{path.suffix}")
        if not cand.exists():
            return cand
        k += 1


def best_rate_pair(scale: float, sr: int) -> tuple[int, int]:
    """Inteiros (B, A) próximos de `sr` com B/A ~= scale (erro tipicamente < 0,01 ppm)."""
    a = np.arange(int(sr * 0.8), int(sr * 1.25), dtype=np.int64)
    b = np.round(a * scale).astype(np.int64)
    err = np.abs(b / a - scale)
    k = int(np.argmin(err))
    return int(b[k]), int(a[k])


# --------------------------------------------------------------------------
# Correção de drift
# --------------------------------------------------------------------------

def drift_candidates(project: Project, min_error_s: float = 0.002) -> list[Clip]:
    return [c for c in project.all_clips()
            if c.is_synced and c.info and not c.info.has_video and c.drift_reliable
            and abs(c.drift) * c.duration >= min_error_s]


def export_drift_corrected(project: Project, out_dir: str, min_error_s: float = 0.002,
                           progress: Optional[ProgressFn] = None,
                           cancel: Optional[threading.Event] = None,
                           log: Optional[Callable[[str], None]] = None) -> dict[int, Corrected]:
    """Regrava os arquivos só-de-áudio com drift relevante, esticando-os para o
    relógio da timeline. Retorna {uid do clipe: Corrected}."""
    ff = require_ffmpeg()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    todo = drift_candidates(project, min_error_s)
    result: dict[int, Corrected] = {}
    for k, c in enumerate(todo):
        if progress:
            progress(k / max(1, len(todo)), f"Corrigindo drift: {c.name}")
        sr = c.info.audio_rate or 48000
        scale = 1.0 + c.drift
        b, a = best_rate_pair(scale, sr)
        codec = "pcm_f32le" if c.info.audio_bits == 32 and "f" in c.info.audio_codec else "pcm_s24le"
        dst = _unique(out / f"{Path(c.path).stem}_driftfix.wav")
        cmd = [ff, "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", c.path,
               "-map", "0:a:0", "-af", f"aresample={b},asetrate={a},aresample={sr}",
               "-c:a", codec, "-rf64", "auto", str(dst)]
        try:
            _run_ffmpeg(cmd, cancel)
            info = probe(str(dst))
            result[c.uid] = Corrected(str(dst), info, b / a)
            if log:
                log(f"Drift corrigido: {c.name} ({c.drift * 1e6:+.2f} ppm) -> {dst.name}")
        except Cancelled:
            dst.unlink(missing_ok=True)
            raise
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"Falha ao corrigir {c.name}: {e}")
    if progress:
        progress(1.0, "Correção de drift concluída")
    return result


# --------------------------------------------------------------------------
# Substituição do áudio da câmera pelo áudio do gravador
# --------------------------------------------------------------------------

def _span(c: Clip) -> tuple[float, float]:
    return c.timeline_time(0.0), c.timeline_time(c.duration)


def export_replaced_audio(project: Project, master_track: str, out_dir: str,
                          keep_camera_audio: bool = True,
                          progress: Optional[ProgressFn] = None,
                          cancel: Optional[threading.Event] = None,
                          log: Optional[Callable[[str], None]] = None) -> list[str]:
    """Gera cópias dos vídeos (sem recodificar a imagem) com o áudio do gravador
    sincronizado na trilha 1 e, opcionalmente, o áudio original da câmera nas
    trilhas seguintes."""
    ff = require_ffmpeg()
    master = project.track(master_track)
    if master is None:
        raise ValueError(f"Faixa '{master_track}' não encontrada.")
    masters = [m for m in master.clips if m.is_synced and m.info and m.info.has_audio]
    videos = [c for c in project.all_clips()
              if c.is_synced and c.info and c.info.has_video and c not in master.clips]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for k, v in enumerate(videos):
        if progress:
            progress(k / max(1, len(videos)), f"Exportando {v.name}")
        v0, v1 = _span(v)
        best, best_ov = None, 0.0
        for m in masters:
            m0, m1 = _span(m)
            ov = min(v1, m1) - max(v0, m0)
            if ov > best_ov:
                best, best_ov = m, ov
        if best is None:
            if log:
                log(f"{v.name}: nenhum áudio de '{master_track}' cobre este clipe — ignorado.")
            continue
        dv = v.duration
        sr = best.info.audio_rate or 48000
        ch = best.info.audio_channels or 2
        # velocidade do gravador em relação ao relógio desta câmera
        scale = (1.0 + best.drift) / (1.0 + v.drift)
        stretch = (v.drift_reliable and best.drift_reliable
                   and abs(scale - 1.0) * dv > 0.001)
        if stretch:
            sm = best.local_time(v.timeline_time(0.0))
            b, a = best_rate_pair(scale, sr)
            speed = f"aresample={b},asetrate={a},aresample={sr},"
        else:
            t_mid = v.timeline_time(dv / 2)
            sm = best.local_time(t_mid) - dv / 2      # início no arquivo do gravador
            scale, speed = 1.0, ""
        cmd = [ff, "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", v.path]
        if sm >= 0:
            cmd += ["-ss", f"{sm:.6f}", "-i", best.path]
            af = speed + "apad"
        else:
            delay = int(round(-sm * scale * sr))
            cmd += ["-i", best.path]
            af = speed + "adelay=" + "|".join([f"{delay}S"] * ch) + ",apad"
        cmd += ["-filter_complex", f"[1:a:0]{af}[ma]", "-map", "0:v:0", "-map", "[ma]"]
        n_cam = v.info.audio_streams if (keep_camera_audio and v.info.has_audio) else 0
        for i in range(n_cam):
            cmd += ["-map", f"0:a:{i}"]
        cmd += ["-c:v", "copy", "-c:a:0", "pcm_s24le"]
        for i in range(n_cam):
            cmd += [f"-c:a:{i + 1}", "pcm_s24le"]
        cmd += ["-metadata:s:a:0", "title=Gravador (sincronizado)",
                "-map_metadata", "0", "-t", f"{dv:.6f}"]
        if v.info.timecode:
            cmd += ["-timecode", v.info.timecode]
        dst = _unique(out / f"{Path(v.path).stem}_sync.mov")
        try:
            _run_ffmpeg(cmd + [str(dst)], cancel)
            written.append(str(dst))
            if log:
                log(f"{v.name} -> {dst.name} (áudio de {best.name} a partir de {sm:+.3f} s)")
        except Cancelled:
            dst.unlink(missing_ok=True)
            raise
        except Exception as e:  # noqa: BLE001
            dst.unlink(missing_ok=True)
            if log:
                log(f"Falha ao exportar {v.name}: {e}")
    if progress:
        progress(1.0, "Exportação de mídia concluída")
    return written
