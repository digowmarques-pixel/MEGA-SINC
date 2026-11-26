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
