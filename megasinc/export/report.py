"""Relatórios CSV/JSON com as posições sincronizadas."""
from __future__ import annotations

import csv
import json
from fractions import Fraction
from typing import Optional

from ..media import seconds_to_tc
from ..project import Project
from ..timeline import DEFAULT_FPS


def _rows(project: Project, fps: Optional[Fraction]):
    fps = fps or DEFAULT_FPS
    synced = [c for c in project.all_clips() if c.is_synced]
    base = min((c.placement() for c in synced), default=0.0)
    for t in project.tracks:
        for c in t.clips:
            pos = c.placement() - base if c.is_synced else None
            yield {
                "faixa": t.name,
                "arquivo": c.name,
                "caminho": c.path,
                "status": c.status,
                "posicao_s": round(pos, 6) if pos is not None else "",
                "posicao_tc": seconds_to_tc(pos, fps) if pos is not None else "",
                "duracao_s": round(c.duration, 3),
                "confianca": round(c.confidence, 1),
                "drift_ppm": round(c.drift * 1e6, 2) if c.drift_reliable else "",
                "observacao": c.message,
            }


def export_csv(project: Project, out_path: str, fps: Optional[Fraction] = None) -> None:
    rows = list(_rows(project, fps))
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["arquivo"],
                           delimiter=";")
        w.writeheader()
        w.writerows(rows)


def export_json(project: Project, out_path: str, fps: Optional[Fraction] = None) -> None:
    data = {"fps": str(fps or DEFAULT_FPS), "clips": list(_rows(project, fps)),
            "project": project.to_dict()}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
