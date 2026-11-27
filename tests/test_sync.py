"""Testes de ponta a ponta com mídia sintética (requer ffmpeg e scipy)."""
import json
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg ausente")

from megasinc.analysis import coarse_match, compute_features, next_fast_len, refine_match  # noqa: E402
from megasinc.engine import SyncEngine, SyncOptions  # noqa: E402
from megasinc.export import export_csv, export_fcpxml, export_replaced_audio, export_xmeml  # noqa: E402
from megasinc.media import frames_to_tc, parse_fps_string, probe, tc_to_frames  # noqa: E402
from megasinc.project import Project, Status, device_prefix  # noqa: E402
from megasinc.timeline import build_timeline  # noqa: E402


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    from make_test_media import main
    out = tmp_path_factory.mktemp("media")
    main(str(out))
    return out


@pytest.fixture(scope="session")
def synced(media, tmp_path_factory):
    p = Project()
    p.add_paths([str(media)])
    opts = SyncOptions(timeline_clock="reference",
                       cache_dir=tmp_path_factory.mktemp("cache"))
    rep = SyncEngine(p, opts).run()
    return p, rep, json.loads((media / "truth.json").read_text()), media


def test_helpers():
    assert device_prefix("CAM_A_0001") == "CAM_A"
    assert device_prefix("ZOOM0003") == "ZOOM"
    assert device_prefix("A001_C002") == "A001_C"
    from fractions import Fraction
    assert parse_fps_string("29.97") == Fraction(30000, 1001)
    assert parse_fps_string("23,976") == Fraction(24000, 1001)
    f = Fraction(30000, 1001)
    for n in (0, 1799, 1800, 17982, 107892, 123456):
        assert tc_to_frames(frames_to_tc(n, f, drop=True), f) == n   # drop-frame
        assert tc_to_frames(frames_to_tc(n, f), f) == n              # non-drop
    for n in (1, 97, 1000, 123457):
        m = next_fast_len(n)
        assert m >= n


def test_offsets_accuracy(synced):
    p, rep, truth, media = synced
    assert rep.synced == 5 and rep.unsynced == 1
    for c in p.all_clips():
        rel = Path(c.path).relative_to(media).as_posix()
        t = truth[rel]
        if t is None:
            assert c.status == Status.UNSYNCED
        else:
            assert c.is_synced, c.name
            assert abs(c.offset - t) < 0.002, (c.name, c.offset, t)   # < 2 ms


def test_drift_detected(synced):
    p, *_ = synced
    camb = next(c for c in p.all_clips() if c.name.startswith("CAMB"))
    assert camb.drift_reliable
    assert abs(camb.drift * 1e6 - (-60)) < 6


def test_exports(synced, tmp_path):
    p, *_ = synced
    tl = build_timeline(p)
    export_fcpxml(tl, str(tmp_path / "a.fcpxml"))
    export_xmeml(tl, str(tmp_path / "a.xml"))
    export_csv(p, str(tmp_path / "a.csv"))
    root = ET.parse(tmp_path / "a.fcpxml").getroot()
    assert root.tag == "fcpxml" and len(root.findall(".//asset-clip")) == 5
    x = ET.parse(tmp_path / "a.xml").getroot()
    assert len(x.findall(".//video/track")) == 2
    assert len(x.findall(".//clipitem")) >= 8


def test_replace_audio(synced, tmp_path):
    p, *_ = synced
    files = export_replaced_audio(p, "REC", str(tmp_path))
    assert len(files) == 3
    for f in files:
        info = probe(f)
        assert info.has_video and info.audio_streams == 2


def test_coarse_and_fine_on_noise():
    rng = np.random.default_rng(0)
    sr = 16000
    x = (rng.standard_normal(sr * 60) * 3000 * (rng.random(sr * 60) > 0.3)).astype(np.int16)
    lag = 12.3456
    y = x[int(lag * sr): int(lag * sr) + sr * 30]
    cm = coarse_match(compute_features(x), compute_features(y))
    assert abs(cm.lag - lag) < 0.02
    fm = refine_match(x, y, cm.lag)
    assert abs(fm.lag - lag) < 1e-3


def test_channel_selection(tmp_path):
    """Canal 1 com ruído forte, canal 2 com o áudio útil: só o canal 2 sincroniza bem."""
    from make_test_media import SR, speechlike
    import wave
    rng = np.random.default_rng(3)
    master = speechlike(60.0, rng)
    ref = tmp_path / "ref"
    cam = tmp_path / "cam"
    ref.mkdir()
    cam.mkdir()

    def write(path, chans):
        data = np.stack([np.clip(c, -1, 1) for c in chans], axis=1)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(len(chans))
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((data * 32767).astype("<i2").tobytes())

    write(ref / "REC_0001.wav", [master])
    seg = master[int(12.5 * SR): int(42.5 * SR)]
    noise = rng.standard_normal(len(seg)).astype(np.float32) * 0.6
    write(cam / "CAM_0001.wav", [noise, seg])

    from megasinc.media import _audio_input_args, channel_count, probe as pr
    info = pr(str(cam / "CAM_0001.wav"))
    assert channel_count(info) == 2
    assert "pan=mono|c0=c1" in _audio_input_args(info, 2)

    p = Project()
    p.add_paths([str(ref), str(cam)])
    p.track("CAM").audio_channel = 2
    SyncEngine(p, SyncOptions(cache_dir=tmp_path / "cache", timeline_clock="reference")).run()
    c = p.track("CAM").clips[0]
    assert c.is_synced and abs(c.offset - 12.5) < 0.002


def test_timecode_modes(tmp_path):
    """Gravador com BWF 01:00:00:00, câmera Y (áudio útil, TC 01:00:20:00) e câmera X
    (só ruído, TC 01:00:10:00). Híbrido: Y pelo áudio, X pelo timecode."""
    import subprocess
    import wave
    from make_test_media import SR, speechlike
    rng = np.random.default_rng(9)
    master = speechlike(60.0, rng)
    raw = tmp_path / "m.wav"
    with wave.open(str(raw), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(master, -1, 1) * 32767).astype("<i2").tobytes())
    (tmp_path / "rec").mkdir()
    (tmp_path / "camx").mkdir()
    (tmp_path / "camy").mkdir()
    ff = ["ffmpeg", "-v", "error", "-y"]
    subprocess.run(ff + ["-i", str(raw), "-c:a", "pcm_s16le", "-write_bext", "1",
                         "-metadata", f"time_reference={3600 * SR}",
                         str(tmp_path / "rec" / "REC_0001.wav")], check=True)

    def cam(path, tc, start, noise=False):
        src = ["-f", "lavfi", "-i", "anoisesrc=d=20:a=0.3:r=48000"] if noise else \
            ["-ss", str(start), "-t", "20", "-i", str(raw)]
        subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=20",
                             *src, "-map", "0:v", "-map", "1:a", "-c:v", "libx264",
                             "-preset", "ultrafast", "-c:a", "pcm_s16le", "-shortest",
                             "-timecode", tc, str(path)], check=True)
    cam(tmp_path / "camy" / "CAMY_0001.mov", "01:00:20:00", 20)
    cam(tmp_path / "camx" / "CAMX_0001.mov", "01:00:10:00", 0, noise=True)

    def run(method):
        p = Project()
        p.add_paths([str(tmp_path / d) for d in ("rec", "camx", "camy")])
        SyncEngine(p, SyncOptions(method=method, timeline_clock="reference",
                                  cache_dir=tmp_path / "cache")).run()
        return {c.name.split("_")[0]: c for c in p.all_clips()}

    a = run("audio")
    assert a["CAMY"].is_synced and not a["CAMX"].is_synced
    h = run("hybrid")
    assert h["CAMX"].is_synced and h["CAMX"].message == "por timecode"
    assert abs(h["CAMX"].offset - h["REC"].offset - 10.0) < 0.05
    assert abs(h["CAMY"].offset - h["REC"].offset - 20.0) < 0.005
    t = run("timecode")
    assert all(c.is_synced for c in t.values())
    assert abs(t["CAMY"].offset - t["REC"].offset - 20.0) < 1e-6
    assert abs(t["CAMX"].offset - t["REC"].offset - 10.0) < 1e-6


def test_multicam_and_mute(synced, tmp_path):
    from fractions import Fraction
    from megasinc.export import export_fcpxml_multicam

    def sec(v):
        return Fraction(v[:-1]) if v.endswith("s") else Fraction(v)

    p, *_ = synced
    tl = build_timeline(p)
    export_fcpxml_multicam(tl, str(tmp_path / "mc.fcpxml"), audio_track="REC")
    root = ET.parse(tmp_path / "mc.fcpxml").getroot()
    mc = root.find(".//media/multicam")
    angles = mc.findall("mc-angle")
    assert [a.get("name") for a in angles] == ["CAMA", "CAMB", "REC"]
    total = sec(root.find(".//project/sequence").get("duration"))
    for a in angles:                      # storyline contínua: offset = soma das durações
        cursor = Fraction(0)
        for el in a:
            assert sec(el.get("offset")) == cursor, (a.get("name"), el.attrib)
            cursor += sec(el.get("duration"))
        assert cursor == total
    srcs = root.findall(".//project//mc-clip/mc-source")
    assert {(s.get("angleID"), s.get("srcEnable")) for s in srcs} == {
        ("angle-1", "video"), ("angle-3", "audio")}
    # silenciar áudio das câmeras
    export_xmeml(tl, str(tmp_path / "m.xml"), mute_camera_audio=True)
    x = ET.parse(tmp_path / "m.xml").getroot()
    states = {(c.find("name").text, c.find("enabled").text)
              for c in x.findall(".//audio/track/clipitem")}
    assert ("CAMB_0001.mov", "FALSE") in states and ("REC_0001.wav", "TRUE") in states
    export_fcpxml(tl, str(tmp_path / "m.fcpxml"), mute_camera_audio=True)
    f = ET.parse(tmp_path / "m.fcpxml").getroot()
    assert all(c.get("srcEnable") == "video" for c in f.findall(".//asset-clip")
               if "CAM" in c.get("name"))
