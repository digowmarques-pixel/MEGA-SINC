"""Interface de linha de comando.

Exemplos:
  megasinc sync /pasta/do/job --fcpxml job.fcpxml --xml job.xml
  megasinc sync --track "Cam A" a1.mov a2.mov --track Zoom zoom.wav --reference Zoom --xml out.xml
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from . import APP_NAME, __version__
from .engine import SyncEngine, SyncOptions
from .export import (export_csv, export_drift_corrected, export_fcpxml, export_json,
                     export_replaced_audio, export_xmeml)
from .media import Cancelled, describe_tools, parse_fps_string
from .project import GROUP_MODES, Project, Status
from .timeline import build_timeline


def _progress_printer():
    last = [-1]

    def cb(frac: float, msg: str) -> None:
        pct = int(frac * 100)
        if pct != last[0]:
            last[0] = pct
            sys.stderr.write(f"\r[{pct:3d}%] {msg[:70]:<70}")
            sys.stderr.flush()
    return cb


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="megasinc",
                                 description=f"{APP_NAME} {__version__} — sincronização multicâmera por áudio")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="abre a interface gráfica")
    sub.add_parser("info", help="mostra onde está o FFmpeg")
    s = sub.add_parser("sync", help="sincroniza arquivos e exporta")
    s.add_argument("inputs", nargs="*", help="arquivos ou pastas")
    s.add_argument("--track", "-t", nargs="+", action="append", metavar=("NOME", "ARQUIVO"),
                   help="define uma faixa manualmente: --track 'Cam A' a.mov b.mov")
    s.add_argument("--group", choices=list(GROUP_MODES), default="auto",
                   help="agrupamento automático das entradas soltas (padrão: auto)")
    s.add_argument("--reference", "-r", help="nome da faixa de referência")
    s.add_argument("--fps", help="quadros/s da timeline (ex.: 23.976, 25, 29.97). Padrão: auto")
    s.add_argument("--clock", choices=["video", "reference"], default="video",
                   help="relógio da timeline: câmeras (video) ou faixa de referência")
    s.add_argument("--same-track", action="store_true", help="compara clipes da mesma faixa")
    s.add_argument("--min-score", type=float, default=SyncOptions.min_score,
                   help="limiar de confiança (padrão: %(default)s)")
    s.add_argument("--name", default="Mega Sinc", help="nome da sequência")
    s.add_argument("--fcpxml", help="exporta FCPXML (Final Cut / DaVinci)")
    s.add_argument("--fcpxml-version", default="1.10", choices=["1.8", "1.9", "1.10", "1.11"])
    s.add_argument("--xml", help="exporta XML FCP7 (Premiere / DaVinci)")
    s.add_argument("--csv", help="exporta relatório CSV")
    s.add_argument("--json", help="exporta relatório JSON")
    s.add_argument("--drift-fix", metavar="PASTA",
                   help="gera WAVs com drift corrigido e usa-os nos XMLs")
    s.add_argument("--replace-audio", metavar="PASTA",
                   help="gera vídeos com o áudio da faixa de referência sincronizado")
    s.add_argument("--master", help="faixa de áudio usada em --replace-audio (padrão: referência)")
    s.add_argument("--include-unsynced", action="store_true",
                   help="coloca os não sincronizados no fim da timeline")
    return ap


def run_sync(args) -> int:
    project = Project()
    if args.inputs:
        project.add_paths(args.inputs, args.group)
    for spec in args.track or []:
        name, files = spec[0], spec[1:]
        tr = project.track(name) or project.add_track(name)
        for c in project.add_paths(files, "single"):
            project.move_clip(c, tr.name)
    project.remove_empty_tracks()
    if not project.all_clips():
        print("Nenhum arquivo de mídia encontrado.", file=sys.stderr)
        return 2
    if args.reference:
        if not project.track(args.reference):
            print(f"Faixa '{args.reference}' não existe. Faixas: "
                  f"{', '.join(t.name for t in project.tracks)}", file=sys.stderr)
            return 2
        project.reference = args.reference

    print("Faixas:")
    for t in project.tracks:
        print(f"  {t.name}: {len(t.clips)} arquivo(s)")
    opts = SyncOptions(min_score=args.min_score, compare_same_track=args.same_track,
                       timeline_clock=args.clock)
    cancel = threading.Event()
    eng = SyncEngine(project, opts, progress=_progress_printer(),
                     log=lambda m: print("\n" + m, file=sys.stderr), cancel=cancel)
    try:
        rep = eng.run()
    except KeyboardInterrupt:
        cancel.set()
        print("\nCancelado.", file=sys.stderr)
        return 130
    except Cancelled:
        return 130
    sys.stderr.write("\n")

    fps = parse_fps_string(args.fps) if args.fps else None
    print(f"\n{'Faixa':<18} {'Arquivo':<32} {'Posição (s)':>12} {'Conf.':>6} {'Drift':>9}  Status")
    synced = [c for c in project.all_clips() if c.is_synced]
    base = min((c.placement() for c in synced), default=0.0)
    for t in project.tracks:
        for c in t.clips:
            pos = f"{c.placement() - base:12.4f}" if c.is_synced else f"{'—':>12}"
            drift = f"{c.drift * 1e6:+7.1f}ppm" if c.drift_reliable and c.drift else f"{'':>9}"
            conf = f"{c.confidence:6.1f}" if c.status == Status.SYNCED else f"{'':>6}"
            print(f"{t.name[:18]:<18} {c.name[:32]:<32} {pos} {conf} {drift}  {c.status}"
                  + (f" ({c.message})" if c.message else ""))
    for w in rep.warnings:
        print("Aviso:", w)

    corrected = {}
    if args.drift_fix:
        corrected = export_drift_corrected(project, args.drift_fix, progress=_progress_printer(),
                                           log=lambda m: print("\n" + m, file=sys.stderr))
        sys.stderr.write("\n")
    tl = build_timeline(project, fps, args.name, corrected, args.include_unsynced)
    if args.fcpxml:
        export_fcpxml(tl, args.fcpxml, version=args.fcpxml_version)
        print(f"FCPXML: {args.fcpxml}")
    if args.xml:
        export_xmeml(tl, args.xml)
        print(f"XML (Premiere/FCP7): {args.xml}")
    if args.csv:
        export_csv(project, args.csv, tl.fps)
        print(f"CSV: {args.csv}")
    if args.json:
        export_json(project, args.json, tl.fps)
        print(f"JSON: {args.json}")
    if args.replace_audio:
        ref = args.master or (project.reference_track().name if project.reference_track() else None)
        if ref:
            files = export_replaced_audio(project, ref, args.replace_audio,
                                          progress=_progress_printer(),
                                          log=lambda m: print("\n" + m, file=sys.stderr))
            sys.stderr.write("\n")
            print(f"{len(files)} vídeo(s) exportado(s) em {Path(args.replace_audio)}")
    return 0 if rep.synced > 0 else 1


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "sync":
        return run_sync(args)
    if args.cmd == "info":
        print(describe_tools())
        return 0
    from .gui.app import run_gui
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
