"""Exportação XML no formato Final Cut Pro 7 (xmeml) — Adobe Premiere Pro e DaVinci Resolve.

Cada câmera vira uma trilha de vídeo; o áudio de cada clipe ocupa até duas
trilhas de áudio (canais 1 e 2). Vídeo e áudio de um mesmo arquivo ficam
vinculados (link). O formato trabalha em quadros inteiros, então a posição é
arredondada para o quadro mais próximo (erro máximo de meio quadro).
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

from ..media import frames_to_tc, nominal_fps, tc_to_frames
from ..timeline import Timeline


def path_url(path: str) -> str:
    p = Path(path).resolve().as_posix()
    if not p.startswith("/"):
        p = "/" + p                      # C:/x -> /C:/x
    return "file://localhost" + quote(p, safe="/:")


def _rate(parent: ET.Element, fps: Fraction) -> None:
    r = ET.SubElement(parent, "rate")
    ET.SubElement(r, "timebase").text = str(nominal_fps(fps))
    ET.SubElement(r, "ntsc").text = "TRUE" if Fraction(fps).denominator == 1001 else "FALSE"


def _text(parent: ET.Element, tag: str, value) -> ET.Element:
    e = ET.SubElement(parent, tag)
    e.text = str(value)
    return e


def _timecode(parent: ET.Element, fps: Fraction, frame: int, tc_str: str | None = None) -> None:
    tc = ET.SubElement(parent, "timecode")
    _rate(tc, fps)
    _text(tc, "string", tc_str or frames_to_tc(frame, fps))
    _text(tc, "frame", frame)
    _text(tc, "displayformat", "NDF")


def export_xmeml(tl: Timeline, out_path: str, mute_camera_audio: bool = False) -> None:
    fps = Fraction(tl.fps)
    mute = mute_camera_audio and any(not i.info.has_video and i.info.has_audio
                                     for i in tl.items)
    root = ET.Element("xmeml", version="4")
    seq = ET.SubElement(root, "sequence", id="sequence-1")
    _text(seq, "name", tl.name)
    total_frames = math.ceil(tl.duration * fps)
    _text(seq, "duration", total_frames)
    _rate(seq, fps)
    _timecode(seq, fps, 0)
    media = ET.SubElement(seq, "media")
    video = ET.SubElement(media, "video")
    vfmt = ET.SubElement(ET.SubElement(video, "format"), "samplecharacteristics")
    _rate(vfmt, fps)
    _text(vfmt, "width", tl.width)
    _text(vfmt, "height", tl.height)
    _text(vfmt, "anamorphic", "FALSE")
    _text(vfmt, "pixelaspectratio", "square")
    _text(vfmt, "fielddominance", "none")
    audio = ET.SubElement(media, "audio")
    _text(audio, "numOutputChannels", 2)
    afmt = ET.SubElement(ET.SubElement(audio, "format"), "samplecharacteristics")
    _text(afmt, "depth", 16)
    _text(afmt, "samplerate", tl.audio_rate)

    # ---- ids e posições -----------------------------------------------------
    file_ids: dict[str, str] = {}
    file_written: set[str] = set()
    n_clip = [0]

    def clip_id() -> str:
        n_clip[0] += 1
        return f"clipitem-{n_clip[0]}"

    plan = []   # (item, start, end, in, out, video_id, [(audio_id, channel, track_key)])
    for it in tl.items:
        start = round(it.start * fps)
        src_frames = math.floor(it.info.duration * fps + 1e-6)
        if src_frames <= 0:
            continue
        vid = clip_id() if it.info.has_video else None
        auds = []
        if it.info.has_audio:
            for ch in range(min(2, max(1, it.info.audio_channels))):
                auds.append((clip_id(), ch + 1))
        plan.append((it, start, start + src_frames, 0, src_frames, vid, auds))
        if it.path not in file_ids:
            file_ids[it.path] = f"file-{len(file_ids) + 1}"

    def add_file(parent: ET.Element, it) -> None:
        fid = file_ids[it.path]
        if fid in file_written:
            ET.SubElement(parent, "file", id=fid)
            return
        file_written.add(fid)
        info = it.info
        f = ET.SubElement(parent, "file", id=fid)
        _text(f, "name", Path(it.path).name)
        _text(f, "pathurl", path_url(it.path))
        frate = Fraction(info.fps) if (info.has_video and info.fps) else fps
        _rate(f, frate)
        _text(f, "duration", math.floor(info.duration * frate + 1e-6))
        tc_frame = 0
        if info.has_video and info.timecode and info.fps:
            try:
                tc_frame = tc_to_frames(info.timecode, info.fps)
            except ValueError:
                tc_frame = 0
        _timecode(f, frate, tc_frame, info.timecode if tc_frame else None)
        fm = ET.SubElement(f, "media")
        if info.has_video:
            sc = ET.SubElement(ET.SubElement(fm, "video"), "samplecharacteristics")
            _rate(sc, frate)
            _text(sc, "width", info.width or tl.width)
            _text(sc, "height", info.height or tl.height)
            _text(sc, "anamorphic", "FALSE")
            _text(sc, "pixelaspectratio", "square")
            _text(sc, "fielddominance", "none")
        if info.has_audio:
            fa = ET.SubElement(fm, "audio")
            sc = ET.SubElement(fa, "samplecharacteristics")
            _text(sc, "depth", info.audio_bits if info.audio_bits in (16, 24, 32) else 16)
            _text(sc, "samplerate", info.audio_rate or 48000)
            _text(fa, "channelcount", info.audio_channels or 2)

    def base_clipitem(parent, cid, it, start, end, cin, cout) -> ET.Element:
        ci = ET.SubElement(parent, "clipitem", id=cid)
        _text(ci, "name", Path(it.path).name)
        _text(ci, "enabled", "TRUE")
        _text(ci, "duration", math.floor(it.info.duration * fps + 1e-6))
        _rate(ci, fps)
        _text(ci, "start", start)
        _text(ci, "end", end)
        _text(ci, "in", cin)
        _text(ci, "out", cout)
        return ci

    def add_links(ci, vid, auds, track_index) -> None:
        if not vid and len(auds) < 2:
            return
        if vid:
            lk = ET.SubElement(ci, "link")
            _text(lk, "linkclipref", vid)
            _text(lk, "mediatype", "video")
            _text(lk, "trackindex", track_index["v"])
            _text(lk, "clipindex", 1)
        for k, (aid, ch) in enumerate(auds):
            lk = ET.SubElement(ci, "link")
            _text(lk, "linkclipref", aid)
            _text(lk, "mediatype", "audio")
            _text(lk, "trackindex", track_index["a"][k])
            _text(lk, "clipindex", 1)
            _text(lk, "groupindex", 1)

    # ---- índices das trilhas -------------------------------------------------
    v_index: dict[tuple[str, int], int] = {}
    a_index: dict[tuple[str, int, int], int] = {}
    nv = na = 0
    for tr in tl.tracks:
        for lane in range(tr.lanes):
            if tr.is_video:
                nv += 1
                v_index[(tr.name, lane)] = nv
            for ch in range(max(1, tr.channels)):
                na += 1
                a_index[(tr.name, lane, ch + 1)] = na

    track_of_item = {}
    for p in plan:
        it, _, _, _, _, vid, auds = p
        track_of_item[id(it)] = {
            "v": v_index.get((it.track, it.lane), 0),
            "a": [a_index.get((it.track, it.lane, ch), 0) for _, ch in auds],
        }

    # ---- trilhas de vídeo ----------------------------------------------------
    v_tracks = {k: ET.SubElement(video, "track") for k in sorted(v_index, key=v_index.get)}
    a_tracks = {k: ET.SubElement(audio, "track") for k in sorted(a_index, key=a_index.get)}

    for it, start, end, cin, cout, vid, auds in sorted(plan, key=lambda p: p[1]):
        ti = track_of_item[id(it)]
        if vid:
            parent = v_tracks[(it.track, it.lane)]
            ci = base_clipitem(parent, vid, it, start, end, cin, cout)
            add_file(ci, it)
            add_links(ci, vid, auds, ti)
        for aid, ch in auds:
            parent = a_tracks[(it.track, it.lane, ch)]
            ci = base_clipitem(parent, aid, it, start, end, cin, cout)
            if mute and it.info.has_video:          # só o áudio do gravador fica ativo
                ci.find("enabled").text = "FALSE"
            add_file(ci, it)
            st = ET.SubElement(ci, "sourcetrack")
            _text(st, "mediatype", "audio")
            _text(st, "trackindex", ch)
            add_links(ci, vid, auds, ti)

    for vt in v_tracks.values():
        _text(vt, "enabled", "TRUE")
        _text(vt, "locked", "FALSE")
    for k, at in a_tracks.items():
        _text(at, "enabled", "TRUE")
        _text(at, "locked", "FALSE")
        _text(at, "outputchannelindex", k[2])

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        f.write(body)
        f.write("\n")
