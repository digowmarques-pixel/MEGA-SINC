"""Monta a timeline (faixas, sub-faixas e posições) a partir do projeto sincronizado."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from .media import MediaInfo
from .project import Clip, Project


@dataclass
class Corrected:
    """Arquivo de áudio regravado com correção de drift."""
    path: str
    info: MediaInfo
    factor: float          # duração nova / duração original


@dataclass
class TimelineItem:
    clip: Clip
    path: str
    info: MediaInfo
    start: float           # segundos na timeline (>= 0)
    duration: float
    track: str
    lane: int = 0
    corrected: bool = False

    @property
    def name(self) -> str:
        from pathlib import Path
        return Path(self.path).stem


@dataclass
class TimelineTrack:
    name: str
    is_video: bool
    lanes: int = 1
    channels: int = 2


@dataclass
class Timeline:
    name: str
    fps: Fraction
    width: int
    height: int
    audio_rate: int
    items: list[TimelineItem] = field(default_factory=list)
    tracks: list[TimelineTrack] = field(default_factory=list)
    shift: float = 0.0     # quanto foi somado às posições para começar em 0

    @property
    def duration(self) -> float:
        return max((it.start + it.duration for it in self.items), default=0.0)


DEFAULT_FPS = Fraction(30000, 1001)


def guess_fps(project: Project) -> Fraction:
    cnt: Counter = Counter()
    for c in project.all_clips():
        if c.is_synced and c.info and c.info.has_video and c.info.fps:
            cnt[c.info.fps] += c.duration
    return cnt.most_common(1)[0][0] if cnt else DEFAULT_FPS


def guess_resolution(project: Project) -> tuple[int, int]:
    cnt: Counter = Counter()
    for c in project.all_clips():
        if c.is_synced and c.info and c.info.has_video and c.info.width:
            cnt[(c.info.width, c.info.height)] += c.duration
    return cnt.most_common(1)[0][0] if cnt else (1920, 1080)


def build_timeline(project: Project, fps: Optional[Fraction] = None, name: str = "Mega Sinc",
                   corrected: Optional[dict[int, Corrected]] = None,
                   include_unsynced: bool = False) -> Timeline:
    corrected = corrected or {}
    fps = fps or guess_fps(project)
    w, h = guess_resolution(project)
    tl = Timeline(name=name, fps=fps, width=w, height=h, audio_rate=48000)

    raw: list[TimelineItem] = []
    for t in project.tracks:
        for c in t.clips:
            if not c.is_synced or not c.info:
                continue
            if c.uid in corrected:
                cc = corrected[c.uid]
                raw.append(TimelineItem(c, cc.path, cc.info, c.offset, cc.info.duration,
                                        t.name, corrected=True))
            else:
                raw.append(TimelineItem(c, c.path, c.info, c.placement(), c.duration, t.name))
    if raw:
        tl.shift = -min(it.start for it in raw)
        for it in raw:
            it.start += tl.shift
    end = max((it.start + it.duration for it in raw), default=0.0)
    if include_unsynced:
        cursor = end + 5.0
        for t in project.tracks:
            for c in t.clips:
                if not c.is_synced and c.info and c.duration > 0:
                    raw.append(TimelineItem(c, c.path, c.info, cursor, c.duration,
                                            "Não sincronizados"))
                    cursor += c.duration + 1.0

    # Ordem das faixas: vídeo primeiro (na ordem do projeto), depois só-áudio.
    order = [t.name for t in project.tracks]
    names = list(dict.fromkeys([it.track for it in raw]))
    names.sort(key=lambda n: (not any(i.track == n and i.info.has_video for i in raw),
                              order.index(n) if n in order else len(order)))
    for n in names:
        items = sorted([i for i in raw if i.track == n], key=lambda i: i.start)
        lane_ends: list[float] = []
        for it in items:
            for k, e in enumerate(lane_ends):
                if it.start >= e - 1e-6:
                    it.lane = k
                    lane_ends[k] = it.start + it.duration
                    break
            else:
                it.lane = len(lane_ends)
                lane_ends.append(it.start + it.duration)
        is_video = any(i.info.has_video for i in items)
        ch = max((min(2, i.info.audio_channels or 0) for i in items), default=0)
        tl.tracks.append(TimelineTrack(n, is_video, max(1, len(lane_ends)), ch))
        tl.items.extend(items)
    rates = Counter(i.info.audio_rate for i in tl.items if i.info.has_audio)
    if rates:
        r = rates.most_common(1)[0][0]
        tl.audio_rate = 48000 if r not in (44100, 48000, 96000) else r
    return tl
