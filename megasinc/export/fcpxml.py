"""Exportação FCPXML (Final Cut Pro X/11 e DaVinci Resolve).

Cada câmera/gravador vira uma "lane" de clipes conectados a um gap na storyline
principal: câmeras acima (lanes positivas), gravadores de áudio abaixo
(lanes negativas). Clipes de vídeo ficam no quadro mais próximo; clipes só de
áudio mantêm precisão de amostra (o ponto de entrada é deslocado por frações de
quadro).
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from ..timeline import Timeline


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


def export_fcpxml(tl: Timeline, out_path: str, version: str = "1.10",
                  event_name: str = "Mega Sinc") -> None:
    fps = Fraction(tl.fps)
    fd = 1 / fps   # duração de um quadro da sequência

    root = ET.Element("fcpxml", version=version)
    res = ET.SubElement(root, "resources")
    formats: dict[tuple, str] = {}
    counter = [0]

    def new_id() -> str:
        counter[0] += 1
        return f"r{counter[0]}"

    def fmt_id(rate: Fraction, w: int, h: int) -> str:
        key = (rate, w, h)
        if key not in formats:
            fid = new_id()
            ET.SubElement(res, "format", id=fid, frameDuration=rt(1 / Fraction(rate)),
                          width=str(w), height=str(h))
            formats[key] = fid
        return formats[key]

    seq_fmt = fmt_id(fps, tl.width, tl.height)

    assets: dict[str, tuple[str, Fraction, Fraction]] = {}   # path -> (id, start, dur)

    def asset_for(item) -> tuple[str, Fraction, Fraction]:
        if item.path in assets:
            return assets[item.path]
        info = item.info
        aid = new_id()
        start = info.start_seconds()
        if info.has_video:
            vfps = Fraction(info.fps or fps)
            dur = Fraction(math.floor(info.duration * vfps + 1e-6)) / vfps
        else:
            sr = info.audio_rate or 48000
            dur = Fraction(int(round(info.duration * sr)), sr)
        attrs = {"id": aid, "name": Path(item.path).stem, "start": rt(start),
                 "duration": rt(dur), "hasVideo": "1" if info.has_video else "0",
                 "hasAudio": "1" if info.has_audio else "0"}
        if info.has_video:
            attrs["format"] = fmt_id(Fraction(info.fps or fps), info.width or tl.width,
                                     info.height or tl.height)
        if info.has_audio:
            attrs.update(audioSources="1", audioChannels=str(info.audio_channels or 2),
                         audioRate=str(info.audio_rate or 48000))
        if version in ("1.8", "1.7", "1.6"):
            attrs["src"] = _file_url(item.path)
            ET.SubElement(res, "asset", **attrs)
        else:
            a = ET.SubElement(res, "asset", **attrs)
            ET.SubElement(a, "media-rep", kind="original-media", src=_file_url(item.path))
        assets[item.path] = (aid, start, dur)
        return assets[item.path]

    # lanes: faixas de vídeo 1, 2, ...; faixas só de áudio -1, -2, ...
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
        aid, a_start, a_dur = asset_for(it)
        info = it.info
        pos = Fraction(it.start).limit_denominator(10 ** 9)
        if info.has_video:
            off_frames = round(pos * fps)
            offset = off_frames * fd
            c_start = a_start
            dur_frames = math.floor(a_dur * fps)
        else:
            sr = info.audio_rate or 48000
            off_frames = math.ceil(pos * fps - Fraction(1, 10 ** 6))
            offset = off_frames * fd
            shift = Fraction(int(round((offset - pos) * sr)), sr)
            c_start = a_start + shift
            dur_frames = math.floor((a_dur - shift) * fps)
        if dur_frames <= 0:
            continue
        clips.append((offset, dur_frames * fd, it, aid, c_start))

    total = max((o + d for o, d, *_ in clips), default=fd)
    total = Fraction(math.ceil(total * fps)) / fps

    lib = ET.SubElement(root, "library")
    ev = ET.SubElement(lib, "event", name=event_name)
    proj = ET.SubElement(ev, "project", name=tl.name)
    seq = ET.SubElement(proj, "sequence", format=seq_fmt, duration=rt(total), tcStart="0s",
                        tcFormat="NDF", audioLayout="stereo",
                        audioRate=_audio_rate_label(tl.audio_rate))
    spine = ET.SubElement(seq, "spine")
    gap = ET.SubElement(spine, "gap", name="Gap", offset="0s", start="0s", duration=rt(total))
    for offset, dur, it, aid, c_start in sorted(clips, key=lambda c: (c[0], lane_of[(c[2].track, c[2].lane)])):
        attrs = {"ref": aid, "lane": str(lane_of[(it.track, it.lane)]), "offset": rt(offset),
                 "name": f"{it.track} | {Path(it.path).stem}", "start": rt(c_start),
                 "duration": rt(dur)}
        if it.info.has_video:
            attrs["format"] = fmt_id(Fraction(it.info.fps or fps), it.info.width or tl.width,
                                     it.info.height or tl.height)
            attrs["tcFormat"] = "NDF"
        if it.info.has_audio:
            attrs["audioRole"] = "dialogue"
        ET.SubElement(gap, "asset-clip", **attrs)

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n')
        f.write(body)
        f.write("\n")
