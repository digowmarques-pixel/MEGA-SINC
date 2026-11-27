"""Motor de sincronização.

Fluxo:
  1. probe dos arquivos (metadados);
  2. extração do áudio (16 kHz mono, cache em disco) e cálculo das features;
  3. correlação grosseira entre todos os pares de clipes de faixas diferentes;
  4. árvore geradora de máxima confiança + verificação de consistência;
  5. refinamento fino (GCC-PHAT) e drift em cada ligação aceita;
  6. mínimos quadrados ponderados para as posições finais;
  7. (opcional) relógio da timeline = relógio das câmeras.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .analysis import coarse_match, compute_features, refine_match
from .media import Cancelled, cleanup_cache, default_cache_dir, probe
from .prepare import prepare_clip
from .project import Clip, Project, Status

ProgressFn = Callable[[float, str], None]
LogFn = Callable[[str], None]


@dataclass
class SyncOptions:
    min_score: float = 9.0          # destaque mínimo do pico de correlação
    min_ratio: float = 1.25         # pico principal / segundo pico
    compare_same_track: bool = False
    refine: bool = True
    timeline_clock: str = "video"   # "video" (câmeras) ou "reference"
    method: str = "audio"           # "audio", "timecode" ou "hybrid" (áudio + timecode)
    use_clock: bool = True          # usar o horário de gravação das câmeras
    guided_score: float = 6.0       # limiar da busca guiada (janela estreita)
    clock_tolerance: float = 3.0    # s: diferença aceita entre relógio e áudio
    workers: int = max(2, min(8, os.cpu_count() or 4))
    cache_dir: Optional[Path] = None


@dataclass
class Edge:
    i: int
    j: int
    lag: float            # tau_i = tau_j + lag  (válido em tau_j = t_ref)
    t_ref: float
    score: float
    ratio: float
    overlap: float
    drift: float = 0.0    # tau_i = L0 + (1+drift) tau_j
    drift_ok: bool = False
    refined: bool = False

    @property
    def l0(self) -> float:
        return self.lag - self.drift * self.t_ref

    def reversed(self) -> "Edge":
        k = 1.0 + self.drift
        t_ref_i = self.t_ref + self.lag
        return Edge(self.j, self.i, -self.lag, t_ref_i, self.score, self.ratio,
                    self.overlap, 1.0 / k - 1.0, self.drift_ok, self.refined)


@dataclass
class SyncReport:
    synced: int = 0
    unsynced: int = 0
    errors: int = 0
    edges_tested: int = 0
    edges_used: int = 0
    elapsed: float = 0.0
    warnings: list[str] = field(default_factory=list)


class SyncEngine:
    def __init__(self, project: Project, options: Optional[SyncOptions] = None,
                 progress: Optional[ProgressFn] = None, log: Optional[LogFn] = None,
                 cancel: Optional[threading.Event] = None) -> None:
        self.project = project
        self.opt = options or SyncOptions()
        self._progress = progress
        self._log = log
        self.cancel = cancel or threading.Event()
        self.cache_dir = Path(self.opt.cache_dir or default_cache_dir())
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pcm: dict[int, np.ndarray] = {}
        self.feat: dict[int, np.ndarray] = {}

    # ------------------------------------------------------------------
    def progress(self, frac: float, msg: str) -> None:
        if self._progress:
            self._progress(max(0.0, min(1.0, frac)), msg)

    def log(self, msg: str) -> None:
        if self._log:
            self._log(msg)

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    # ------------------------------------------------------------------
    def run(self) -> SyncReport:
        t_start = time.time()
        rep = SyncReport()
        cleanup_cache(self.cache_dir)
        clips = self.project.all_clips()
        for c in clips:
            c.reset()
        if not clips:
            raise ValueError("Nenhum arquivo no projeto.")

        self._probe_all(clips, 0.0, 0.05)
        if self.opt.method == "timecode":
            self._apply_timecode(clips, only_missing=False)
        else:
            self._run_audio(clips, rep)
            if self.opt.method == "hybrid":
                self._apply_timecode(clips, only_missing=True)
        rep.synced = sum(1 for c in clips if c.is_synced)
        rep.unsynced = sum(1 for c in clips if c.status == Status.UNSYNCED)
        rep.errors = sum(1 for c in clips if c.status == Status.ERROR)
        self._check_overlaps(rep)
        rep.elapsed = time.time() - t_start
        self.progress(1.0, "Concluído")
        self.log(f"Sincronizados: {rep.synced} | não sincronizados: {rep.unsynced} | "
                 f"erros: {rep.errors} | {rep.elapsed:.1f} s")
        return rep

    def _run_audio(self, clips: list[Clip], rep: SyncReport) -> None:
        self._analyze_all(clips, 0.05, 0.55)
        usable = [c for c in clips if c.uid in self.feat and self.feat[c.uid].shape[1] > 0]
        for c in clips:
            if c.status == Status.PENDING and c not in usable:
                c.status = Status.UNSYNCED
                c.message = c.message or "sem áudio utilizável"
        if not usable:
            if self.opt.method == "hybrid":
                return
            raise ValueError("Nenhum clipe com áudio para analisar.")

        track_idx = {}
        for ti, t in enumerate(self.project.tracks):
            for c in t.clips:
                track_idx[c.uid] = ti
        n_tracks_used = len({track_idx[c.uid] for c in usable})
        same_track = self.opt.compare_same_track or n_tracks_used == 1
        if n_tracks_used == 1 and len(usable) > 1:
            self.log("Só há uma faixa: comparando clipes da mesma faixa entre si.")

        pairs = [(a, b) for a in range(len(usable)) for b in range(a + 1, len(usable))
                 if same_track or track_idx[usable[a].uid] != track_idx[usable[b].uid]]
        rep.edges_tested = len(pairs)
        edges = self._coarse_all(usable, pairs, 0.55, 0.80)
        self._usable = usable
        self._track_idx = track_idx
        self._side_groups = []
        if self.opt.use_clock:
            edges = self._clock_filter(usable, edges, track_idx)

        ref_track = self.project.reference_track()
        ref_uids = {c.uid for c in ref_track.clips} if ref_track else set()
        solution = self._solve(usable, edges, ref_uids, rep)
        if solution is None:
            for c in usable:
                c.status = Status.UNSYNCED
                c.message = "nenhuma correspondência encontrada"
        elif self.opt.use_clock:
            self._clock_pass(usable, track_idx)

    # ------------------------------------------------------------------
    @staticmethod
    def tc_start(c: Clip) -> Optional[float]:
        """Início do clipe pelo timecode (vídeo) ou BWF (áudio), em segundos."""
        i = c.info
        if i is None:
            return None
        if (i.has_video and i.timecode and i.fps) or (not i.has_video and i.time_reference):
            return float(i.start_seconds())
        return None

    def _apply_timecode(self, clips: list[Clip], only_missing: bool) -> None:
        tcs = {c.uid: self.tc_start(c) for c in clips}
        with_tc = [c for c in clips if c.status != Status.ERROR and tcs[c.uid] is not None]
        delta: Optional[float] = None
        if only_missing:
            anchors = [c for c in clips if c.is_synced and tcs[c.uid] is not None]
            targets = [c for c in with_tc if not c.is_synced]
            if not targets:
                return
            if anchors:
                d = np.array([c.offset - tcs[c.uid] for c in anchors])
                delta = float(np.median(d))
                spread = float(np.max(np.abs(d - delta)))
                if spread > 0.1:
                    self.log(f"Timecode não bate com o áudio (diferença de até {spread:.2f} s) — "
                             "os clipes restantes NÃO foram posicionados por timecode.")
                    return
            elif any(c.is_synced for c in clips):
                self.log("Nenhum clipe sincronizado pelo áudio tem timecode — não dá para "
                         "combinar áudio e timecode.")
                return
        else:
            targets = with_tc
            for c in clips:
                if c.status != Status.ERROR and tcs[c.uid] is None:
                    c.status = Status.UNSYNCED
                    c.message = "sem timecode"
            if not targets:
                raise ValueError("Nenhum arquivo tem timecode (ou BWF) para sincronizar.")
        if delta is None:
            delta = -min(tcs[c.uid] for c in targets)
            ref = self.project.reference_track()
            pool = [c for c in targets if ref and c in ref.clips] or targets
            root = max(pool, key=lambda c: c.duration)
        else:
            root = None
        for c in targets:
            c.offset = tcs[c.uid] + delta
            c.drift, c.drift_reliable, c.confidence = 0.0, False, 0.0
            c.status = Status.REFERENCE if c is root else Status.SYNCED
            c.message = "por timecode"
        self.log(f"{len(targets)} clipe(s) posicionado(s) por timecode.")

    # ------------------------------------------------------------------
    def _probe_all(self, clips: list[Clip], p0: float, p1: float) -> None:
        todo = [c for c in clips if c.info is None]
        if not todo:
            return
        self.progress(p0, "Lendo metadados…")
        with ThreadPoolExecutor(self.opt.workers) as ex:
            futs = {ex.submit(probe, c.path): c for c in todo}
            for k, f in enumerate(as_completed(futs)):
                self.check()
                c = futs[f]
                try:
                    c.info = f.result()
                except Exception as e:  # noqa: BLE001
                    c.status = Status.ERROR
                    c.message = str(e)
                    self.log(f"Erro ao ler {c.name}: {e}")
                self.progress(p0 + (p1 - p0) * (k + 1) / len(todo), f"Metadados: {c.name}")

    def _analyze_one(self, c: Clip, report: Callable[[float], None]) -> None:
        tr = self.project.track_of(c)
        pcm, feat = prepare_clip(c, self.cache_dir, cancel=self.cancel, progress=report,
                                 channel=tr.audio_channel if tr else None)
        if pcm is not None:
            self.pcm[c.uid] = pcm
            self.feat[c.uid] = feat

    def _analyze_all(self, clips: list[Clip], p0: float, p1: float) -> None:
        todo = [c for c in clips if c.info is not None and c.status != Status.ERROR]
        for c in todo:
            if not c.info.has_audio:
                c.status = Status.UNSYNCED
                c.message = "arquivo sem áudio"
        todo = [c for c in todo if c.info.has_audio]
        if not todo:
            return
        total = sum(max(c.duration, 1.0) for c in todo)
        done = defaultdict(float)
        lock = threading.Lock()

        def make_report(c: Clip):
            def rep(f: float) -> None:
                with lock:
                    done[c.uid] = f * max(c.duration, 1.0)
                    frac = sum(done.values()) / total
                self.progress(p0 + (p1 - p0) * frac, f"Analisando áudio: {c.name}")
            return rep

        with ThreadPoolExecutor(self.opt.workers) as ex:
            futs = {ex.submit(self._analyze_one, c, make_report(c)): c for c in todo}
            for f in as_completed(futs):
                c = futs[f]
                try:
                    f.result()
                except Cancelled:
                    self.cancel.set()
                    raise
                except Exception as e:  # noqa: BLE001
                    c.status = Status.ERROR
                    c.message = f"falha ao extrair áudio: {e}"
                    self.log(f"Erro em {c.name}: {e}")
                make_report(c)(1.0)
        self.check()

    # ------------------------------------------------------------------
    def _coarse_all(self, usable: list[Clip], pairs, p0: float, p1: float) -> list[Edge]:
        edges: list[Edge] = []
        if not pairs:
            return edges
        self.progress(p0, f"Comparando {len(pairs)} pares de clipes…")

        def work(pair):
            a, b = pair
            m = coarse_match(self.feat[usable[a].uid], self.feat[usable[b].uid])
            return pair, m

        with ThreadPoolExecutor(self.opt.workers) as ex:
            futs = [ex.submit(work, p) for p in pairs]
            for k, f in enumerate(as_completed(futs)):
                if self.cancel.is_set():
                    for ff in futs:
                        ff.cancel()
                    raise Cancelled()
                (a, b), m = f.result()
                if m and m.score >= self.opt.min_score and m.ratio >= self.opt.min_ratio:
                    la = usable[a].duration
                    t0 = max(0.0, m.lag)
                    t1 = min(la, m.lag + usable[b].duration)
                    edges.append(Edge(a, b, m.lag, (t0 + t1) / 2 - m.lag, m.score, m.ratio,
                                      m.overlap))
                if k % 10 == 0 or k == len(futs) - 1:
                    self.progress(p0 + (p1 - p0) * (k + 1) / len(futs),
                                  f"Comparando pares ({k + 1}/{len(futs)})")
        self.log(f"{len(edges)} correspondências candidatas em {len(pairs)} pares.")
        return edges

    # ------------------------------------------------------------------
    def _spanning_forest(self, n: int, edges: list[Edge]) -> list[Edge]:
        """Árvore geradora de máxima confiança que recusa ligações impossíveis:
        dois clipes da mesma câmera não podem se sobrepor."""
        usable = getattr(self, "_usable", None)
        tidx = getattr(self, "_track_idx", {})
        parent = list(range(n))
        members = {k: [k] for k in range(n)}
        rel = [0.0] * n                    # posição relativa à raiz do grupo

        def find(x: int) -> int:
            while parent[x] != x:
                x = parent[x]
            return x

        tree = []
        rejected = 0
        for e in sorted(edges, key=lambda e: (e.refined, e.drift_ok, e.score), reverse=True):
            ri, rj = find(e.i), find(e.j)
            if ri == rj:
                continue
            shift = rel[e.i] + e.lag - rel[e.j]      # posições do grupo de j no grupo de i
            if usable is not None and self._groups_conflict(members[ri], members[rj], rel,
                                                            shift, usable, tidx):
                rejected += 1
                continue
            for m in members[rj]:
                rel[m] += shift
                parent[m] = ri
            members[ri] += members.pop(rj)
            parent[rj] = ri
            tree.append(e)
        if rejected:
            self.log(f"{rejected} correspondência(s) recusada(s): sobreporiam clipes da "
                     "mesma câmera.")
        return tree

    @staticmethod
    def _groups_conflict(a, b, rel, shift, usable, tidx, tol: float = 1.0) -> bool:
        by_track: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for m in a:
            by_track[tidx.get(usable[m].uid, -1)].append((rel[m], rel[m] + usable[m].duration))
        for m in b:
            t = tidx.get(usable[m].uid, -1)
            s0 = rel[m] + shift
            s1 = s0 + usable[m].duration
            for x0, x1 in by_track.get(t, ()):
                if min(s1, x1) - max(s0, x0) > tol:
                    return True
        return False

    @staticmethod
    def _propagate(n: int, tree: list[Edge], roots: list[int]):
        adj: dict[int, list[Edge]] = defaultdict(list)
        for e in tree:
            adj[e.i].append(e)
            adj[e.j].append(e.reversed())
        pos: dict[int, float] = {}
        drift: dict[int, float] = {}
        rel: dict[int, bool] = {}
        conf: dict[int, float] = {}
        comp: dict[int, int] = {}
        for r in roots:
            if r in pos:
                continue
            pos[r], drift[r], rel[r], conf[r], comp[r] = 0.0, 0.0, True, float("inf"), r
            q = deque([r])
            while q:
                i = q.popleft()
                for e in adj[i]:       # e: tau_i = L0 + (1+d) tau_j
                    j = e.j
                    if j in pos:
                        continue
                    pos[j] = pos[i] + (1.0 + drift[i]) * e.l0
                    drift[j] = (1.0 + drift[i]) * (1.0 + e.drift) - 1.0
                    rel[j] = rel[i] and e.drift_ok
                    conf[j] = e.score
                    comp[j] = r
                    q.append(j)
        return pos, drift, rel, conf, comp

    @staticmethod
    def _residual(e: Edge, pos, drift) -> float:
        tj = pos[e.j] + (1.0 + drift[e.j]) * e.t_ref
        tau_i = (tj - pos[e.i]) / (1.0 + drift[e.i])
        return tau_i - (e.t_ref + e.lag)

    def _choose_roots(self, usable: list[Clip], tree: list[Edge], ref_uids: set[int]) -> list[int]:
        n = len(usable)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for e in tree:
            parent[find(e.i)] = find(e.j)
        comps: dict[int, list[int]] = defaultdict(list)
        for k in range(n):
            comps[find(k)].append(k)

        def comp_rank(members):
            has_ref = any(usable[m].uid in ref_uids for m in members)
            return (len(members) > 1, has_ref, sum(usable[m].duration for m in members))
        ordered = sorted(comps.values(), key=comp_rank, reverse=True)
        roots = []
        for members in ordered:
            refs = [m for m in members if usable[m].uid in ref_uids]
            pool = refs or members
            roots.append(max(pool, key=lambda m: usable[m].duration))
        return roots

    def _solve(self, usable: list[Clip], edges: list[Edge], ref_uids: set[int],
               rep: SyncReport):
        n = len(usable)
        if n == 1:
            c = usable[0]
            c.offset, c.status, c.confidence = 0.0, Status.REFERENCE, 0.0
            return True
        if not edges:
            return None
        self.check()
        # --- etapa grosseira: árvore + consistência ----------------------
        tree = self._spanning_forest(n, edges)
        roots = self._choose_roots(usable, tree, ref_uids)
        pos, drift, _, _, _ = self._propagate(n, tree, roots)
        cand = [e for e in edges if e.i in pos and e.j in pos
                and abs(self._residual(e, pos, drift)) < 0.30]
        # --- refinamento fino ------------------------------------------
        if self.opt.refine:
            self.progress(0.80, f"Refinando {len(cand)} correspondências…")

            def work(e: Edge):
                return e, refine_match(self.pcm[usable[e.i].uid], self.pcm[usable[e.j].uid], e.lag)

            done = 0
            with ThreadPoolExecutor(self.opt.workers) as ex:
                futs = [ex.submit(work, e) for e in cand]
                for f in as_completed(futs):
                    if self.cancel.is_set():
                        for ff in futs:
                            ff.cancel()
                        raise Cancelled()
                    e, fm = f.result()
                    if fm is not None and abs(fm.lag - e.lag) < 0.30 + 0.001 * e.overlap:
                        e.lag, e.t_ref = fm.lag, fm.t_ref
                        e.drift, e.drift_ok, e.refined = fm.drift, fm.drift_ok, True
                    done += 1
                    self.progress(0.80 + 0.15 * done / max(1, len(cand)),
                                  f"Refinando ({done}/{len(cand)})")
        # --- solução final ---------------------------------------------
        tree = self._spanning_forest(n, cand)
        roots = self._choose_roots(usable, tree, ref_uids)
        pos, drift, rel, conf, comp = self._propagate(n, tree, roots)
        tol = 0.004 if self.opt.refine else 0.05
        used = [e for e in cand if e.i in pos and e.j in pos and comp[e.i] == comp[e.j]
                and abs(self._residual(e, pos, drift)) < tol]
        pos = self._least_squares(n, used, pos, drift, comp, roots)
        rep.edges_used = len(used)

        main_root = roots[0]
        main_comp = {k for k in pos if comp[k] == main_root}

        # --- relógio da timeline -----------------------------------------
        if self.opt.timeline_clock == "video":
            vd = [(drift[k], usable[k].duration) for k in main_comp
                  if usable[k].has_video and rel.get(k) and usable[k].duration > 30]
            if vd:
                ds = np.array([d for d, _ in vd])
                ws = np.array([w for _, w in vd])
                order = np.argsort(ds)
                cw = np.cumsum(ws[order])
                dv = float(ds[order][np.searchsorted(cw, cw[-1] / 2)])
                if abs(dv) > 1e-6:
                    self.log(f"Relógio da timeline ajustado às câmeras ({dv * 1e6:+.1f} ppm "
                             "em relação à referência).")
                    for k in main_comp:
                        pos[k] = pos[k] / (1.0 + dv)
                        drift[k] = (1.0 + drift[k]) / (1.0 + dv) - 1.0

        for k, c in enumerate(usable):
            if k in main_comp:
                p_k, d = float(pos[k]), float(drift[k])
                if not rel.get(k):
                    # drift desconhecido: alinha exatamente no meio do clipe
                    p_k += d * c.duration / 2
                    d = 0.0
                # pos[k] refere-se ao 1º sample de áudio; o NLE posiciona pelo vídeo
                delay = c.info.audio_delay if (c.info and c.info.has_video) else 0.0
                c.offset = p_k - (1.0 + d) * delay
                c.drift = d
                c.drift_reliable = bool(rel.get(k))
                c.confidence = float(conf[k]) if np.isfinite(conf[k]) else 0.0
                c.status = Status.REFERENCE if k == main_root else Status.SYNCED
                c.message = ""
                if abs(c.drift) * c.duration > 0.002:
                    c.message = (f"drift {c.drift * 1e6:+.1f} ppm "
                                 f"({abs(c.drift) * c.duration * 1000:.0f} ms no clipe)")
            else:
                c.status = Status.UNSYNCED
                c.message = ("sincroniza só com outros clipes isolados" if k in pos and
                             any(comp[m] == comp[k] for m in pos if m != k)
                             else "nenhuma correspondência encontrada")
        groups: dict[int, list[int]] = defaultdict(list)
        for k in pos:
            if k not in main_comp:
                groups[comp[k]].append(k)
        self._side_groups = [(mem, {m: float(pos[m]) for m in mem})
                             for mem in groups.values() if len(mem) > 1]
        return True

    @staticmethod
    def _least_squares(n, used: list[Edge], pos, drift, comp, roots):
        """Mínimos quadrados ponderados das posições usando todas as ligações
        consistentes (cada componente tem sua raiz fixada em 0)."""
        keys = sorted(pos)
        idx = {k: m for m, k in enumerate(keys)}
        root_set = [r for r in roots if r in idx]
        rows = len(used) + len(root_set)
        if not used:
            return pos
        a = np.zeros((rows, len(keys)))
        b = np.zeros(rows)
        for r, e in enumerate(used):
            w = np.sqrt(e.score) * (10.0 if e.refined else 1.0)
            # p_j - p_i = (1+d_i)(t_ref + lag) - (1+d_j) t_ref
            a[r, idx[e.j]] = w
            a[r, idx[e.i]] = -w
            b[r] = w * ((1.0 + drift[e.i]) * (e.t_ref + e.lag) - (1.0 + drift[e.j]) * e.t_ref)
        for k, root in enumerate(root_set):
            a[len(used) + k, idx[root]] = 1e4
        x0 = np.array([pos[k] for k in keys])
        sol, *_ = np.linalg.lstsq(a, b - a @ x0, rcond=None)
        x = x0 + sol
        return {k: float(x[idx[k]]) for k in keys}

    # ------------------------------------------------------------------
    # Relógio das câmeras (horário de gravação)
    # ------------------------------------------------------------------
    @staticmethod
    def _created(c: Clip) -> Optional[float]:
        return c.info.created if c.info else None

    @staticmethod
    def _pcm_start(c: Clip) -> float:
        delay = c.info.audio_delay if (c.info and c.info.has_video) else 0.0
        return c.offset + (1.0 + c.drift) * delay

    def _clock_filter(self, usable: list[Clip], edges: list[Edge], tidx) -> list[Edge]:
        """A diferença entre os relógios de duas câmeras é constante: ligações
        verdadeiras concordam nela; as falsas (música repetida, ruído) não."""
        tol = self.opt.clock_tolerance
        by_pair: dict[tuple[int, int], list[tuple[Edge, float]]] = defaultdict(list)
        keep: list[Edge] = []
        for e in edges:
            ci, cj = self._created(usable[e.i]), self._created(usable[e.j])
            ti, tj = tidx[usable[e.i].uid], tidx[usable[e.j].uid]
            if ci is None or cj is None or ti == tj:
                keep.append(e)
                continue
            v = (cj - ci) - e.lag             # relógio(Tj) - relógio(Ti)
            key = (ti, tj) if ti < tj else (tj, ti)
            by_pair[key].append((e, v if ti < tj else -v))
        dropped = 0
        for key, lst in by_pair.items():
            vals = np.array([v for _, v in lst])
            w = np.array([e.score for e, _ in lst])
            support = np.array([w[np.abs(vals - v) <= tol].sum() for v in vals])
            center = vals[int(np.argmax(support))]
            near = np.abs(vals - center) <= tol
            if len(lst) >= 2 and near.sum() < 2:
                keep += [e for e, _ in lst]          # sem consenso: não dá para julgar
                continue
            consensus = float(np.median(vals[near]))
            for (e, v) in lst:
                if abs(v - consensus) <= tol:
                    keep.append(e)
                else:
                    dropped += 1
        if dropped:
            self.log(f"{dropped} correspondência(s) descartada(s) por não baterem com o "
                     "horário de gravação das câmeras.")
        return keep

    def _track_clock_offsets(self, usable, tidx) -> dict[int, float]:
        offs: dict[int, list[float]] = defaultdict(list)
        for c in usable:
            cr = self._created(c)
            if c.is_synced and not c.estimated and cr is not None:
                offs[tidx[c.uid]].append(cr - self._pcm_start(c))
        res = {}
        for t, v in offs.items():
            v = np.array(v)
            med = float(np.median(v))
            good = v[np.abs(v - med) <= self.opt.clock_tolerance]
            if len(good) >= max(1, 0.6 * len(v)):
                res[t] = float(np.median(good))
        return res

    def _same_track_free(self, c: Clip, start: float, usable, tidx, tol: float = 1.0) -> bool:
        t = tidx[c.uid]
        for o in usable:
            if o is c or not o.is_synced or tidx[o.uid] != t:
                continue
            s0 = self._pcm_start(o)
            if min(start + c.duration, s0 + o.duration) - max(start, s0) > tol:
                return False
        return True

    def _place(self, c: Clip, pcm_start: float, status_msg: str, estimated: bool,
               confidence: float = 0.0) -> None:
        delay = c.info.audio_delay if (c.info and c.info.has_video) else 0.0
        c.offset = pcm_start - delay
        c.drift, c.drift_reliable = 0.0, False
        c.status = Status.SYNCED
        c.message = status_msg
        c.estimated = estimated
        c.confidence = confidence

    def _guided(self, c: Clip, est: float, usable, tidx) -> Optional[tuple[float, float]]:
        """Procura `c` só perto de `est` (posição prevista pelo relógio)."""
        if c.uid not in self.feat:
            return None
        w = self.opt.clock_tolerance + 1.0
        best = None
        for s in usable:
            if not s.is_synced or s.estimated or tidx[s.uid] == tidx[c.uid] \
                    or s.uid not in self.feat:
                continue
            s0 = self._pcm_start(s)
            if min(est + c.duration + w, s0 + s.duration) - max(est - w, s0) < 2.0:
                continue
            lag_exp = est - s0
            m = coarse_match(self.feat[s.uid], self.feat[c.uid], min_overlap_s=2.0,
                             lag_window=(lag_exp - w, lag_exp + w))
            if m is None or m.score < self.opt.guided_score or m.ratio < 1.1:
                continue
            lag = m.lag
            fm = refine_match(self.pcm[s.uid], self.pcm[c.uid], m.lag)
            if fm is not None and abs(fm.lag - m.lag) < 0.3:
                lag = fm.lag + (0.0 - fm.t_ref) * fm.drift if fm.drift_ok else fm.lag
            start = s0 + (1.0 + s.drift) * lag
            if best is None or m.score > best[1]:
                best = (start, m.score)
        return best

    def _clock_pass(self, usable: list[Clip], tidx) -> None:
        """Usa o horário de gravação para: (1) buscar, numa janela estreita, clipes que
        a busca geral não achou; (2) posicionar grupos isolados e clipes sem áudio em
        comum com as outras câmeras."""
        placed_guided = placed_groups = placed_clock = 0
        for _round in range(max(4, 2 * len(self._side_groups) + 4)):
            offs = self._track_clock_offsets(usable, tidx)
            if not offs:
                return
            changed = False
            # (1) busca guiada para cada clipe ainda solto
            for c in usable:
                cr = self._created(c)
                if c.is_synced or cr is None or tidx[c.uid] not in offs:
                    continue
                est = cr - offs[tidx[c.uid]]
                g = self._guided(c, est, usable, tidx)
                if g and self._same_track_free(c, g[0], usable, tidx):
                    self._place(c, g[0], "encontrado pela busca guiada (horário da câmera)",
                                False, g[1])
                    placed_guided += 1
                    changed = True
            if changed:
                continue
            # (2) grupos isolados (sincronizados entre si pelo áudio)
            for members, gpos in self._side_groups:
                if any(usable[m].is_synced for m in members):
                    continue
                ests = [self._created(usable[m]) - offs[tidx[usable[m].uid]] - gpos[m]
                        for m in members if self._created(usable[m]) is not None
                        and tidx[usable[m].uid] in offs]
                if not ests or np.ptp(ests) > 2 * self.opt.clock_tolerance:
                    continue
                shift = float(np.median(ests))
                # tenta ancorar o grupo pelo áudio a algo já posicionado (busca guiada)
                anchor = None
                for m in members:
                    g = self._guided(usable[m], gpos[m] + shift, usable, tidx)
                    if g and (anchor is None or g[1] > anchor[1]):
                        anchor = (g[0] - gpos[m], g[1])
                precise = anchor is not None and abs(anchor[0] - shift) <= \
                    2 * self.opt.clock_tolerance
                if precise:
                    shift = anchor[0]
                if all(self._same_track_free(usable[m], gpos[m] + shift, usable, tidx)
                       for m in members):
                    msg = ("grupo sincronizado pelo áudio" if precise else
                           "sincronizado pelo áudio com o par; grupo posicionado pelo "
                           "horário da câmera")
                    for m in members:
                        self._place(usable[m], gpos[m] + shift, msg, False,
                                    anchor[1] if precise else 0.0)
                    placed_groups += len(members)
                    changed = True
                    break          # recalcula relógios e tenta os próximos com este como âncora
            if changed:
                continue
            # (3) clipes sem nenhum áudio em comum: posição pelo relógio
            in_groups = {usable[m].uid for members, _ in self._side_groups for m in members
                         if not any(usable[x].is_synced for x in members)}
            for c in usable:
                cr = self._created(c)
                if c.is_synced or cr is None or tidx[c.uid] not in offs or c.uid in in_groups:
                    continue
                est = cr - offs[tidx[c.uid]]
                overlaps_other = any(
                    o.is_synced and tidx[o.uid] != tidx[c.uid] and
                    min(est + c.duration, self._pcm_start(o) + o.duration)
                    - max(est, self._pcm_start(o)) > 2.0 for o in usable)
                if not overlaps_other and self._same_track_free(c, est, usable, tidx):
                    self._place(c, est, "sem áudio em comum — posicionado pelo horário da "
                                "câmera", True)
                    placed_clock += 1
                    changed = True
                elif overlaps_other:
                    c.message = "não encontrou áudio em comum na posição prevista pelo horário"
            if not changed:
                break
        if placed_guided or placed_groups or placed_clock:
            self.log(f"Horário das câmeras: {placed_guided} pela busca guiada, "
                     f"{placed_groups} em grupos isolados, {placed_clock} sem áudio em comum.")

    def _check_overlaps(self, rep: SyncReport) -> None:
        for t in self.project.tracks:
            synced = sorted([c for c in t.clips if c.is_synced], key=lambda c: c.placement())
            for a, b in zip(synced, synced[1:]):
                ov = a.placement() + a.duration - b.placement()
                if ov > 1.0:
                    msg = (f"Faixa '{t.name}': {a.name} e {b.name} se sobrepõem {ov:.1f} s "
                           "(confira se são do mesmo aparelho).")
                    rep.warnings.append(msg)
                    self.log("Aviso: " + msg)
