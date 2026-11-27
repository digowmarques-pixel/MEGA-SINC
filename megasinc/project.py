"""Modelo de dados: projeto, faixas (câmeras/gravadores) e clipes."""
from __future__ import annotations

import itertools
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .media import MediaInfo, is_audio_ext, is_media_file

_ids = itertools.count(1)


class Status:
    PENDING = "pendente"
    REFERENCE = "referência"
    SYNCED = "sincronizado"
    UNSYNCED = "não sincronizado"
    ERROR = "erro"


@dataclass
class Clip:
    path: str
    uid: int = field(default_factory=lambda: next(_ids))
    info: Optional[MediaInfo] = None
    status: str = Status.PENDING
    message: str = ""
    # Resultado: tempo na timeline T(tau) = offset + (1 + drift) * tau
    offset: Optional[float] = None
    drift: float = 0.0
    drift_reliable: bool = False
    confidence: float = 0.0
    # transitório (não salvo no projeto): preparação e forma de onda
    prep: float = field(default=0.0, repr=False, compare=False)
    wave: Optional[object] = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def duration(self) -> float:
        return self.info.duration if self.info else 0.0

    @property
    def has_video(self) -> bool:
        if self.info:
            return self.info.has_video
        return not is_audio_ext(self.path)

    @property
    def has_audio(self) -> bool:
        return self.info.has_audio if self.info else True

    @property
    def is_synced(self) -> bool:
        return self.offset is not None and self.status in (Status.SYNCED, Status.REFERENCE)

    def placement(self) -> Optional[float]:
        """Posição (s) para colocar o clipe SEM correção de velocidade: minimiza o
        erro máximo de drift ao longo do clipe (alinha pelo meio)."""
        if self.offset is None:
            return None
        return self.offset + self.drift * self.duration / 2

    def timeline_time(self, tau: float) -> float:
        return self.offset + (1.0 + self.drift) * tau

    def local_time(self, t: float) -> float:
        return (t - self.offset) / (1.0 + self.drift)

    def reset(self) -> None:
        self.status = Status.PENDING
        self.message = ""
        self.offset = None
        self.drift = 0.0
        self.drift_reliable = False
        self.confidence = 0.0


@dataclass
class Track:
    name: str
    clips: list[Clip] = field(default_factory=list)
    audio_channel: Optional[int] = None    # None = mistura de todos os canais

    @property
    def has_video(self) -> bool:
        return any(c.has_video for c in self.clips)

    @property
    def total_duration(self) -> float:
        return sum(c.duration for c in self.clips)


# --------------------------------------------------------------------------
# Agrupamento automático
# --------------------------------------------------------------------------

GROUP_MODES = {
    "auto": "Automático",
    "folder": "Por pasta",
    "file": "Um arquivo por faixa",
    "single": "Tudo em uma faixa",
}

_TRAIL = re.compile(r"[\d\s_\-\.#()\[\]]+$")


def device_prefix(stem: str) -> str:
    """'CAM_A_0001' -> 'CAM_A', 'ZOOM0003' -> 'ZOOM', 'A001_C002' -> 'A001_C'."""
    p = _TRAIL.sub("", stem).strip(" _-.")
    return p or stem


def group_key(path: str, mode: str) -> tuple[str, str]:
    """Retorna (chave, nome sugerido) da faixa para um arquivo."""
    p = Path(path)
    folder = p.parent.name or str(p.parent)
    kind = "a" if is_audio_ext(p) else "v"
    if mode == "file":
        return str(p), p.stem
    if mode == "single":
        return "single", "Faixa 1"
    if mode == "folder":
        return f"{p.parent}|{kind}", folder + (" (áudio)" if kind == "a" else "")
    prefix = device_prefix(p.stem)
    return f"{p.parent}|{prefix.lower()}|{kind}", prefix


def expand_paths(paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        p = str(p)
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for f in sorted(files):
                    fp = os.path.join(root, f)
                    if is_media_file(fp):
                        out.append(fp)
        elif os.path.isfile(p) and is_media_file(p):
            out.append(p)
    return out


class Project:
    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self.reference: Optional[str] = None   # nome da faixa de referência
        self._keys: dict[str, str] = {}         # chave de agrupamento -> nome da faixa

    # ---- consulta --------------------------------------------------------
    def all_clips(self) -> list[Clip]:
        return [c for t in self.tracks for c in t.clips]

    def track(self, name: str) -> Optional[Track]:
        return next((t for t in self.tracks if t.name == name), None)

    def track_of(self, clip: Clip) -> Optional[Track]:
        return next((t for t in self.tracks if clip in t.clips), None)

    def clip_by_uid(self, uid: int) -> Optional[Clip]:
        return next((c for c in self.all_clips() if c.uid == uid), None)

    def reference_track(self) -> Optional[Track]:
        """Faixa de referência escolhida; senão, o gravador de áudio mais longo; senão,
        a faixa com maior duração total."""
        if self.reference and self.track(self.reference):
            return self.track(self.reference)
        tracks = [t for t in self.tracks if t.clips]
        if not tracks:
            return None
        audio_only = [t for t in tracks if not t.has_video]
        pool = audio_only or tracks
        return max(pool, key=lambda t: t.total_duration)

    # ---- edição ----------------------------------------------------------
    def unique_track_name(self, base: str) -> str:
        names = {t.name for t in self.tracks}
        if base not in names:
            return base
        for i in itertools.count(2):
            cand = f"{base} {i}"
            if cand not in names:
                return cand
        raise AssertionError

    def add_track(self, name: str) -> Track:
        t = Track(self.unique_track_name(name))
        self.tracks.append(t)
        return t

    def add_paths(self, paths: Iterable[str], mode: str = "auto") -> list[Clip]:
        existing = {os.path.abspath(c.path) for c in self.all_clips()}
        added: list[Clip] = []
        for p in expand_paths(paths):
            ap = os.path.abspath(p)
            if ap in existing:
                continue
            existing.add(ap)
            key, label = group_key(ap, mode)
            tname = self._keys.get(key)
            track = self.track(tname) if tname else None
            if track is None:
                track = self.add_track(label)
                self._keys[key] = track.name
            clip = Clip(path=ap)
            track.clips.append(clip)
            added.append(clip)
        for t in self.tracks:
            t.clips.sort(key=lambda c: c.name.lower())
        return added

    def move_clip(self, clip: Clip, track_name: str) -> None:
        src = self.track_of(clip)
        dst = self.track(track_name) or self.add_track(track_name)
        if src is dst:
            return
        if src:
            src.clips.remove(clip)
        dst.clips.append(clip)
        dst.clips.sort(key=lambda c: c.name.lower())

    def remove_clip(self, clip: Clip) -> None:
        t = self.track_of(clip)
        if t:
            t.clips.remove(clip)

    def remove_empty_tracks(self) -> None:
        self.tracks = [t for t in self.tracks if t.clips]
        if self.reference and not self.track(self.reference):
            self.reference = None

    def rename_track(self, old: str, new: str) -> str:
        t = self.track(old)
        if not t or not new.strip() or new == old:
            return old
        new = self.unique_track_name(new.strip())
        t.name = new
        if self.reference == old:
            self.reference = new
        for k, v in list(self._keys.items()):
            if v == old:
                self._keys[k] = new
        return new

    def clear(self) -> None:
        self.tracks.clear()
        self.reference = None
        self._keys.clear()

    def reset_results(self) -> None:
        for c in self.all_clips():
            c.reset()

    # ---- persistência ----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": 1,
            "reference": self.reference,
            "tracks": [
                {"name": t.name, "audio_channel": t.audio_channel, "clips": [
                    {"path": c.path, "status": c.status, "message": c.message,
                     "offset": c.offset, "drift": c.drift,
                     "drift_reliable": c.drift_reliable, "confidence": c.confidence,
                     "info": c.info.to_dict() if c.info else None}
                    for c in t.clips]}
                for t in self.tracks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        p = cls()
        p.reference = d.get("reference")
        for td in d.get("tracks", []):
            t = Track(td["name"], audio_channel=td.get("audio_channel"))
            for cd in td.get("clips", []):
                c = Clip(path=cd["path"])
                c.status = cd.get("status", Status.PENDING)
                c.message = cd.get("message", "")
                c.offset = cd.get("offset")
                c.drift = cd.get("drift", 0.0)
                c.drift_reliable = cd.get("drift_reliable", False)
                c.confidence = cd.get("confidence", 0.0)
                if cd.get("info"):
                    c.info = MediaInfo.from_dict(cd["info"])
                t.clips.append(c)
            p.tracks.append(t)
        return p

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
