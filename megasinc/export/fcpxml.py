"""Exportação FCPXML (Final Cut Pro X/11 e DaVinci Resolve).

Dois formatos:
* **timeline** — cada câmera/gravador vira uma "lane" de clipes conectados a um gap
  na storyline principal (câmeras acima, gravadores abaixo);
* **multicâmera** — um clipe multicâmera (`multicam`) em que cada câmera é um ângulo
  e o gravador fornece o áudio, pronto para cortar.

Clipes de vídeo ficam no quadro mais próximo; clipes só de áudio mantêm precisão
de amostra (o ponto de entrada é deslocado por frações de quadro).
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Optional

from ..timeline import Timeline, TimelineItem


def rt(x: Fraction) -> str:
    x = Fraction(x)
    if x.denominator == 1:
        return f"{x.numerator}s"
    return f"{x.numerator}/{x.denominator}s"


def _audio_rate_label(rate: int) -> str:
    return {32000: "32k", 44100: "44.1k", 48000: "48k", 88200: "88.2k", 96000: "96k",
            176400: "176.4k", 192000: "192k"}.get(rate, "48k")


def _file_url(path: str) -> str:
    return Path(path).resolve().as_uri()


def has_external_audio(tl: Timeline) -> bool:
    return any(not it.info.has_video and it.info.has_audio for it in tl.items)


class _Builder:
    """Recursos (formatos/assets) e geometria dos clipes compartilhados."""

    def __init__(self, tl: Timeline, version: str) -> None:
        self.tl = tl
        self.version = version
        self.fps = Fraction(tl.fps)
        self.fd = 1 / self.fps
        self.root = ET.Element("fcpxml", version=version)
        self.res = ET.SubElement(self.root, "resources")
        self.formats: dict[tuple, str] = {}
        self.assets: dict[str, tuple[str, Fraction, Fraction]] = {}
        self.n = 0
        self.seq_fmt = self.fmt_id(self.fps, tl.width, tl.height)

    def new_id(self) -> str:
        self.n += 1
        return f"r{self.n}"

    def fmt_id(self, rate: Fraction, w: int, h: int) -> str:
        key = (Fraction(rate), w, h)
        if key not in self.formats:
            fid = self.new_id()
            ET.SubElement(self.res, "format", id=fid, frameDuration=rt(1 / Fraction(rate)),
                          width=str(w), height=str(h))
            self.formats[key] = fid
        return self.formats[key]

    def clip_fmt(self, it: TimelineItem) -> str:
        return self.fmt_id(Fraction(it.info.fps or self.fps), it.info.width or self.tl.width,
                           it.info.height or self.tl.height)

    def asset(self, it: TimelineItem) -> tuple[str, Fraction, Fraction]:
        if it.path in self.assets:
            return self.assets[it.path]
        info = it.info
        aid = self.new_id()
        start = info.start_seconds()
        if info.has_video:
            vfps = Fraction(info.fps or self.fps)
            dur = Fraction(math.floor(info.duration * vfps + 1e-6)) / vfps
        else:
            sr = info.audio_rate or 48000
            dur = Fraction(int(round(info.duration * sr)), sr)
        attrs = {"id": aid, "name": Path(it.path).stem, "start": rt(start), "duration": rt(dur),
                 "hasVideo": "1" if info.has_video else "0",
                 "hasAudio": "1" if info.has_audio else "0"}
        if info.has_video:
            attrs["format"] = self.clip_fmt(it)
        if info.has_audio:
            attrs.update(audioSources="1", audioChannels=str(info.audio_channels or 2),
                         audioRate=str(info.audio_rate or 48000))
        if self.version in ("1.8", "1.7", "1.6"):
            attrs["src"] = _file_url(it.path)
            ET.SubElement(self.res, "asset", **attrs)
        else:
            a = ET.SubElement(self.res, "asset", **attrs)
            ET.SubElement(a, "media-rep", kind="original-media", src=_file_url(it.path))
        self.assets[it.path] = (aid, start, dur)
        return self.assets[it.path]

    def geometry(self, it: TimelineItem) -> Optional[tuple[int, int, Fraction]]:
        """(início em quadros, duração em quadros, ponto de entrada na mídia)."""
        _, a_start, a_dur = self.asset(it)
        pos = Fraction(it.start).limit_denominator(10 ** 9)
        if it.info.has_video:
            off = round(pos * self.fps)
            c_start = a_start
            dur = math.floor(a_dur * self.fps)
        else:
            sr = it.info.audio_rate or 48000
            off = math.ceil(pos * self.fps - Fraction(1, 10 ** 6))
            shift = Fraction(int(round((off * self.fd - pos) * sr)), sr)
            c_start = a_start + shift
            dur = math.floor((a_dur - shift) * self.fps)
        if dur <= 0:
            return None
        return off, dur, c_start

    def clip_attrs(self, it: TimelineItem, name: str, offset: Fraction, start: Fraction,
                   dur: Fraction, mute_audio: bool) -> dict:
        aid = self.asset(it)[0]
        attrs = {"ref": aid, "offset": rt(offset), "name": name, "start": rt(start),
                 "duration": rt(dur)}
        if it.info.has_video:
            attrs["format"] = self.clip_fmt(it)
            attrs["tcFormat"] = "NDF"
            if mute_audio and it.info.has_audio:
                attrs["srcEnable"] = "video"
        if it.info.has_audio:
            attrs["audioRole"] = "dialogue"
        return attrs

    def sequence(self, parent: ET.Element, total: Fraction) -> ET.Element:
        return ET.SubElement(parent, "sequence", format=self.seq_fmt, duration=rt(total),
                             tcStart="0s", tcFormat="NDF", audioLayout="stereo",
                             audioRate=_audio_rate_label(self.tl.audio_rate))

    def write(self, out_path: str) -> None:
        ET.indent(self.root, space="  ")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n')
            f.write(ET.tostring(self.root, encoding="unicode"))
            f.write("\n")


def export_fcpxml(tl: Timeline, out_path: str, version: str = "1.10",
                  event_name: str = "Mega Sinc", mute_camera_audio: bool = False) -> None:
    """Timeline com uma lane por câmera/gravador."""
    b = _Builder(tl, version)
    mute = mute_camera_audio and has_external_audio(tl)
    lane_of: dict[tuple[str, int], int] = {}
    v_lane, a_lane = 1, -1
    for tr in tl.tracks:
        for k in range(tr.lanes):
            if tr.is_video:
                lane_of[(tr.name, k)] = v_lane
                v_lane += 1
            else:
                lane_of[(tr.name, k)] = a_lane
                a_lane -= 1
    clips = []
    for it in tl.items:
        g = b.geometry(it)
        if g:
            clips.append((g[0], g[1], g[2], it))
    total_f = max((o + d for o, d, *_ in clips), default=1)
    total = Fraction(total_f) * b.fd
    lib = ET.SubElement(b.root, "library")
    ev = ET.SubElement(lib, "event", name=event_name)
    proj = ET.SubElement(ev, "project", name=tl.name)
    spine = ET.SubElement(b.sequence(proj, total), "spine")
    gap = ET.SubElement(spine, "gap", name="Gap", offset="0s", start="0s", duration=rt(total))
    for off, dur, c_start, it in sorted(clips, key=lambda c: (c[0], lane_of[(c[3].track,
                                                                              c[3].lane)])):
        attrs = b.clip_attrs(it, f"{it.track} | {Path(it.path).stem}", off * b.fd, c_start,
                             dur * b.fd, mute)
        attrs = {"ref": attrs.pop("ref"), "lane": str(lane_of[(it.track, it.lane)]), **attrs}
        ET.SubElement(gap, "asset-clip", **attrs)
    b.write(out_path)


def export_fcpxml_multicam(tl: Timeline, out_path: str, version: str = "1.10",
                           event_name: str = "Mega Sinc",
                           audio_track: Optional[str] = None) -> None:
    """Clipe multicâmera: um ângulo por câmera/gravador (sub-faixas viram ângulos
    extras). O vídeo ativo é a 1ª câmera e o áudio ativo vem de `audio_track`
    (padrão: o primeiro gravador só de áudio; sem gravador, a 1ª câmera)."""
    b = _Builder(tl, version)
    angles: list[tuple[str, str, bool, list]] = []      # (nome, id, vídeo?, clipes)
    for tr in tl.tracks:
        for k in range(tr.lanes):
            items = [it for it in tl.items if it.track == tr.name and it.lane == k]
            if items:
                name = tr.name if k == 0 else f"{tr.name} ({k + 1})"
                angles.append((name, f"angle-{len(angles) + 1}", tr.is_video, items))
    if not angles:
        raise ValueError("Nada para exportar.")
    total_f = 1
    built = []
    for name, aid, is_video, items in angles:
        geo = []
        for it in sorted(items, key=lambda i: i.start):
            g = b.geometry(it)
            if g:
                geo.append((g[0], g[1], g[2], it))
        built.append((name, aid, is_video, geo))
        total_f = max([total_f] + [o + d for o, d, *_ in geo])
    total = Fraction(total_f) * b.fd

    mc_id = b.new_id()
    media = ET.SubElement(b.res, "media", id=mc_id, name=f"{tl.name} Multicâmera")
    mc = ET.SubElement(media, "multicam", format=b.seq_fmt, tcStart="0s", tcFormat="NDF")
    for name, aid, _is_video, geo in built:
        ang = ET.SubElement(mc, "mc-angle", name=name, angleID=aid)
        cursor = 0
        for off, dur, c_start, it in geo:
            if off < cursor:                 # arredondamento encostou no clipe anterior
                cut = cursor - off
                if it.info.has_video:
                    vfps = Fraction(it.info.fps or b.fps)
                    c_start += Fraction(math.ceil(cut * b.fd * vfps)) / vfps
                else:
                    c_start += cut * b.fd
                dur -= cut
                off = cursor
                if dur <= 0:
                    continue
            if off > cursor:
                ET.SubElement(ang, "gap", name="Gap", offset=rt(cursor * b.fd), start="3600s",
                              duration=rt((off - cursor) * b.fd))
            ET.SubElement(ang, "asset-clip", **b.clip_attrs(
                it, Path(it.path).stem, off * b.fd, c_start, dur * b.fd, False))
            cursor = off + dur
        if cursor < total_f:
            ET.SubElement(ang, "gap", name="Gap", offset=rt(cursor * b.fd), start="3600s",
                          duration=rt((total_f - cursor) * b.fd))

    video_angle = next((aid for _n, aid, v, _g in built if v), built[0][1])
    audio_angle = None
    if audio_track:
        audio_angle = next((aid for n, aid, _v, _g in built if n == audio_track), None)
    if audio_angle is None:
        audio_angle = next((aid for _n, aid, v, _g in built if not v), video_angle)

    def mc_clip(parent: ET.Element, offset: str = "0s") -> None:
        clip = ET.SubElement(parent, "mc-clip", ref=mc_id, offset=offset,
                             name=f"{tl.name} Multicâmera", start="0s", duration=rt(total))
        if video_angle == audio_angle:
            ET.SubElement(clip, "mc-source", angleID=video_angle, srcEnable="all")
        else:
            ET.SubElement(clip, "mc-source", angleID=video_angle, srcEnable="video")
            ET.SubElement(clip, "mc-source", angleID=audio_angle, srcEnable="audio")

    lib = ET.SubElement(b.root, "library")
    ev = ET.SubElement(lib, "event", name=event_name)
    mc_clip(ev)                                   # clipe no navegador
    proj = ET.SubElement(ev, "project", name=tl.name)
    spine = ET.SubElement(b.sequence(proj, total), "spine")
    mc_clip(spine)
    b.write(out_path)
