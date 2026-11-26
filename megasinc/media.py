"""Localização do FFmpeg, leitura de metadados (probe) e extração de áudio."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# Taxa usada internamente para análise (precisão de 1/16000 s = 0,06 ms).
ANALYSIS_SR = 16000

VIDEO_EXTS = {
    ".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mts", ".m2ts", ".mkv", ".3gp",
    ".webm", ".mpg", ".mpeg", ".wmv", ".dv", ".braw", ".r3d", ".insv", ".lrv",
}
AUDIO_EXTS = {
    ".wav", ".bwf", ".aif", ".aiff", ".mp3", ".m4a", ".aac", ".flac", ".ogg",
    ".opus", ".wma", ".caf",
}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS


class FFmpegError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


_POPEN_KW: dict = {}
if sys.platform == "win32":  # evita abrir janelas de console no Windows
    _POPEN_KW["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_media_file(path: str | os.PathLike) -> bool:
    p = Path(path)
    return p.suffix.lower() in MEDIA_EXTS and not p.name.startswith("._")


def is_audio_ext(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTS


# --------------------------------------------------------------------------
# Localização dos executáveis
# --------------------------------------------------------------------------

def _exe(name: str) -> str:
    return name + (".exe" if sys.platform == "win32" else "")


def _bundle_dirs() -> list[Path]:
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs += [Path(meipass) / "ffmpeg", Path(meipass)]
        exe_dir = Path(sys.executable).resolve().parent
        dirs += [
            exe_dir / "ffmpeg",
            exe_dir,
            exe_dir.parent / "Resources" / "ffmpeg",   # macOS .app
            exe_dir.parent / "Frameworks" / "ffmpeg",
        ]
    dirs.append(Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg")
    return dirs


_EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/opt/local/bin"]
_tool_cache: dict[str, Optional[str]] = {}


def find_tool(name: str) -> Optional[str]:
    """Procura ffmpeg/ffprobe: variável de ambiente, pacote do app, PATH, imageio-ffmpeg."""
    if name in _tool_cache:
        return _tool_cache[name]
    result: Optional[str] = None
    env = os.environ.get(f"MEGASINC_{name.upper()}")
    candidates = [Path(env)] if env else []
    candidates += [d / _exe(name) for d in _bundle_dirs()]
    for c in candidates:
        if c.is_file():
            result = str(c)
            break
    if result is None and name == "ffprobe":
        ff = find_tool("ffmpeg")
        if ff:
            sib = Path(ff).parent / _exe("ffprobe")
            if sib.is_file():
                result = str(sib)
    if result is None:
        result = shutil.which(name) or shutil.which(
            name, path=os.pathsep.join(_EXTRA_PATH))
    if result is None and name == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore
            p = imageio_ffmpeg.get_ffmpeg_exe()
            if p and os.path.isfile(p):
                result = p
        except Exception:
            pass
    _tool_cache[name] = result
    return result


def require_ffmpeg() -> str:
    ff = find_tool("ffmpeg")
    if not ff:
        raise FFmpegError(
            "FFmpeg não encontrado. Instale o FFmpeg (ex.: 'brew install ffmpeg' no Mac "
            "ou 'winget install ffmpeg' no Windows) ou defina MEGASINC_FFMPEG.")
    return ff


def _run(cmd: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, **_POPEN_KW)


# --------------------------------------------------------------------------
# Taxas de quadro e timecode
# --------------------------------------------------------------------------

STANDARD_RATES = [
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
    Fraction(30), Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60),
    Fraction(100), Fraction(120000, 1001), Fraction(120), Fraction(15), Fraction(12),
]


def snap_fps(value) -> Optional[Fraction]:
    try:
        f = Fraction(value) if not isinstance(value, str) else Fraction(value.strip())
    except (ValueError, ZeroDivisionError):
        return None
    if f <= 0 or f > 1000:
        return None
    for std in STANDARD_RATES:
        if abs(float(f) / float(std) - 1) < 0.002:
            return std
    return f.limit_denominator(1001)


def parse_fps_string(s: str) -> Optional[Fraction]:
    """'29.97' / '30000/1001' / '25' -> Fraction."""
    s = s.strip().replace(",", ".")
    if not s or s.lower() == "auto":
        return None
    try:
        if "/" in s:
            n, d = s.split("/", 1)
            return snap_fps(Fraction(int(n), int(d)))
        return snap_fps(Fraction(s))
    except (ValueError, ZeroDivisionError):
        return None


def fps_label(f: Optional[Fraction]) -> str:
    if f is None:
        return "—"
    v = float(f)
    return f"{v:.3f}".rstrip("0").rstrip(".")


def nominal_fps(fps: Fraction) -> int:
    return int(round(float(fps)))


def tc_to_frames(tc: str, fps: Fraction) -> int:
    parts = [int(p) for p in re.split(r"[:;.,]", tc.strip()) if p != ""]
    if len(parts) != 4:
        raise ValueError(f"timecode inválido: {tc}")
    h, m, s, f = parts
    nom = nominal_fps(fps)
    drop = (";" in tc or "," in tc) and fps.denominator == 1001
    if drop and nom in (30, 60):
        dropf = 2 * (nom // 30)
        total_min = 60 * h + m
        return (3600 * h + 60 * m + s) * nom + f - dropf * (total_min - total_min // 10)
    return ((h * 60 + m) * 60 + s) * nom + f


def frames_to_tc(frames: int, fps: Fraction, drop: bool = False) -> str:
    nom = nominal_fps(fps)
    frames = max(0, int(frames))
    if drop and fps.denominator == 1001 and nom in (30, 60):
        dropf = 2 * (nom // 30)
        per_10 = nom * 600 - dropf * 9
        per_min = nom * 60 - dropf
        d, mm = divmod(frames, per_10)
        if mm > dropf:
            frames += dropf * 9 * d + dropf * ((mm - dropf) // per_min)
        else:
            frames += dropf * 9 * d
        sep = ";"
    else:
        sep = ":"
    f = frames % nom
    s = (frames // nom) % 60
    m = (frames // (nom * 60)) % 60
    h = frames // (nom * 3600)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{f:02d}"


def seconds_to_tc(seconds: float, fps: Fraction) -> str:
    return frames_to_tc(int(round(seconds * float(fps))), fps)


# --------------------------------------------------------------------------
# Metadados
# --------------------------------------------------------------------------

@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    width: int = 0
    height: int = 0
    fps: Optional[Fraction] = None
    audio_rate: int = 48000
    audio_channels: int = 0
    audio_streams: int = 0
    audio_bits: int = 16
    timecode: Optional[str] = None
    time_reference: Optional[int] = None  # BWF: amostras desde a meia-noite
    video_codec: str = ""
    audio_codec: str = ""
    make: str = ""
    model: str = ""
    # início do áudio em relação ao início do vídeo dentro do arquivo (s)
    audio_delay: float = 0.0

    def start_seconds(self) -> Fraction:
        """Início da mídia segundo o timecode/BWF embutido (0 se não houver)."""
        if self.has_video and self.timecode and self.fps:
            try:
                return Fraction(tc_to_frames(self.timecode, self.fps)) / self.fps
            except ValueError:
                return Fraction(0)
        if not self.has_video and self.time_reference and self.audio_rate:
            return Fraction(int(self.time_reference), int(self.audio_rate))
        return Fraction(0)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["fps"] = str(self.fps) if self.fps else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MediaInfo":
        d = dict(d)
        d["fps"] = Fraction(d["fps"]) if d.get("fps") else None
        known = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in known})


def _channels_from_layout(text: str) -> int:
    t = text.lower()
    m = re.search(r"(\d+)\s*channels", t)
    if m:
        return int(m.group(1))
    table = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "4.0": 4, "5.0": 5,
             "5.1": 6, "6.1": 7, "7.1": 8, "hexagonal": 6, "octagonal": 8}
    for k, v in table.items():
        if re.search(rf"\b{re.escape(k)}\b", t):
            return v
    return 2


def probe(path: str) -> MediaInfo:
    path = str(path)
    if not os.path.isfile(path):
        raise FFmpegError(f"Arquivo não encontrado: {path}")
    ffprobe = find_tool("ffprobe")
    if ffprobe:
        try:
            return _probe_ffprobe(ffprobe, path)
        except (FFmpegError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
    return _probe_ffmpeg(require_ffmpeg(), path)


def _bits_from_fmt(fmt: str, codec: str) -> int:
    m = re.search(r"(\d+)", codec or "")
    if codec.startswith("pcm_") and m:
        return int(m.group(1))
    if "flt" in (fmt or "") or "s32" in (fmt or ""):
        return 32
    if "s24" in (fmt or ""):
        return 24
    return 16


def _float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _probe_ffprobe(ffprobe: str, path: str) -> MediaInfo:
    r = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_format",
              "-show_streams", path], timeout=60)
    if r.returncode != 0:
        raise FFmpegError(r.stderr.decode("utf-8", "replace").strip() or "ffprobe falhou")
    data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    fmt = data.get("format", {}) or {}
    ftags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    info = MediaInfo(path=path)
    durations = []
    v_start = a_start = None
    for st in data.get("streams", []):
        ctype = st.get("codec_type")
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        disp = st.get("disposition") or {}
        if "timecode" in tags and not info.timecode:
            info.timecode = tags["timecode"]
        if ctype == "video" and not disp.get("attached_pic") and not info.has_video:
            info.has_video = True
            info.width = int(st.get("width") or 0)
            info.height = int(st.get("height") or 0)
            info.video_codec = st.get("codec_name", "")
            fr = None
            for key in ("avg_frame_rate", "r_frame_rate"):
                v = st.get(key)
                if v and v not in ("0/0", "0/1"):
                    fr = snap_fps(v)
                    if fr:
                        break
            info.fps = fr
            v_start = _float(st.get("start_time"))
            if st.get("duration"):
                durations.append(float(st["duration"]))
        elif ctype == "audio" and int(st.get("channels") or 0) > 0:
            info.audio_streams += 1
            if not info.has_audio:
                info.has_audio = True
                info.audio_rate = int(st.get("sample_rate") or 48000)
                info.audio_channels = int(st.get("channels") or 2)
                info.audio_codec = st.get("codec_name", "")
                info.audio_bits = int(st.get("bits_per_raw_sample") or 0) or \
                    _bits_from_fmt(st.get("sample_fmt", ""), info.audio_codec)
                a_start = _float(st.get("start_time"))
            if st.get("duration"):
                durations.append(float(st["duration"]))
    if fmt.get("duration"):
        info.duration = float(fmt["duration"])
    elif durations:
        info.duration = max(durations)
    if v_start is not None and a_start is not None and abs(a_start - v_start) < 5.0:
        info.audio_delay = a_start - v_start
    info.timecode = info.timecode or ftags.get("timecode")
    tr = ftags.get("time_reference")
    if tr and str(tr).isdigit():
        info.time_reference = int(tr)
    info.make = ftags.get("com.apple.quicktime.make") or ftags.get("make") or ""
    info.model = ftags.get("com.apple.quicktime.model") or ftags.get("model") or ""
    if not info.has_video and not info.has_audio:
        raise FFmpegError("Nenhuma trilha de áudio ou vídeo encontrada")
    return info


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_STREAM_RE = re.compile(
    r"Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^)]*\))?:\s*(Video|Audio|Data|Subtitle):\s*(.*)")


def _probe_ffmpeg(ffmpeg: str, path: str) -> MediaInfo:
    """Fallback quando não há ffprobe: interpreta a saída de 'ffmpeg -i'."""
    r = _run([ffmpeg, "-hide_banner", "-nostdin", "-i", path], timeout=60)
    text = r.stderr.decode("utf-8", "replace")
    if "Invalid data" in text or "No such file" in text:
        raise FFmpegError(text.strip().splitlines()[-1] if text.strip() else "ffmpeg falhou")
    info = MediaInfo(path=path)
    m = _DUR_RE.search(text)
    if m:
        info.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    for kind, desc in _STREAM_RE.findall(text):
        if kind == "Video" and "attached pic" not in desc and not info.has_video:
            info.has_video = True
            info.video_codec = desc.split()[0].strip(",")
            sm = re.search(r"\b(\d{2,5})x(\d{2,5})\b", desc)
            if sm:
                info.width, info.height = int(sm.group(1)), int(sm.group(2))
            fm = re.search(r"([\d.]+)\s*fps", desc) or re.search(r"([\d.]+)\s*tbr", desc)
            if fm:
                info.fps = snap_fps(Fraction(fm.group(1)))
        elif kind == "Audio":
            info.audio_streams += 1
            if not info.has_audio:
                info.has_audio = True
                info.audio_codec = desc.split()[0].strip(",")
                hm = re.search(r"(\d+)\s*Hz", desc)
                if hm:
                    info.audio_rate = int(hm.group(1))
                after_hz = desc.split("Hz", 1)[-1]
                info.audio_channels = _channels_from_layout(after_hz.split(",")[1] if "," in after_hz else after_hz)
                bm = re.search(r"\b(s16|s24|s32|flt|fltp|s16p|s32p|dbl)\b", desc)
                info.audio_bits = _bits_from_fmt(bm.group(1) if bm else "", info.audio_codec)
    tm = re.search(r"timecode\s*:\s*(\d\d[:;]\d\d[:;]\d\d[:;.,]\d\d)", text)
    if tm:
        info.timecode = tm.group(1)
    trm = re.search(r"time_reference\s*:\s*(\d+)", text)
    if trm:
        info.time_reference = int(trm.group(1))
    if not info.has_video and not info.has_audio:
        raise FFmpegError("Nenhuma trilha de áudio ou vídeo encontrada")
    return info


# --------------------------------------------------------------------------
# Extração de áudio para análise (com cache em disco)
# --------------------------------------------------------------------------

def default_cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "mega_sinc_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_cache(cache_dir: Path, max_age_days: float = 7) -> None:
    limit = time.time() - max_age_days * 86400
    try:
        for f in cache_dir.iterdir():
            try:
                if f.stat().st_mtime < limit:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def cache_key(path: str, sr: int = ANALYSIS_SR) -> str:
    st = os.stat(path)
    raw = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|{sr}|v1"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _audio_input_args(info: MediaInfo) -> list[str]:
    if info.audio_streams > 1:
        ins = "".join(f"[0:a:{i}]" for i in range(info.audio_streams))
        return ["-filter_complex",
                f"{ins}amix=inputs={info.audio_streams}:duration=longest[aout]",
                "-map", "[aout]"]
    return ["-map", "0:a:0"]


def extract_pcm(info: MediaInfo, out_path: Path, sr: int = ANALYSIS_SR,
                cancel: Optional[threading.Event] = None,
                progress: Optional[Callable[[float], None]] = None) -> int:
    """Decodifica o áudio para PCM 16-bit mono em `out_path`. Retorna nº de amostras."""
    ffmpeg = require_ffmpeg()
    tmp = out_path.with_suffix(".part")
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", info.path,
           "-vn", "-sn", "-dn", *_audio_input_args(info),
           "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", "-f", "s16le",
           "-progress", "pipe:1", "-nostats", "-y", str(tmp)]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **_POPEN_KW)
    err_chunks: list[bytes] = []
    t = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    t.start()
    try:
        for raw in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise Cancelled()
            line = raw.decode("ascii", "ignore").strip()
            if progress and line.startswith("out_time_us=") and info.duration > 0:
                try:
                    us = int(line.split("=", 1)[1])
                    progress(min(1.0, us / 1e6 / info.duration))
                except ValueError:
                    pass
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t.join(timeout=5)
    if cancel is not None and cancel.is_set():
        tmp.unlink(missing_ok=True)
        raise Cancelled()
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        msg = b"".join(err_chunks).decode("utf-8", "replace").strip()
        raise FFmpegError(msg.splitlines()[-1] if msg else "falha ao decodificar o áudio")
    os.replace(tmp, out_path)
    return os.path.getsize(out_path) // 2


def open_pcm(path: Path) -> np.ndarray:
    if os.path.getsize(path) == 0:
        return np.zeros(0, dtype=np.int16)
    return np.memmap(path, dtype=np.int16, mode="r")


def describe_tools() -> str:
    ff = find_tool("ffmpeg") or "não encontrado"
    fp = find_tool("ffprobe") or "não encontrado (usando ffmpeg -i)"
    return f"ffmpeg: {ff}\nffprobe: {fp}"
