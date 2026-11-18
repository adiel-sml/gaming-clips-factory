"""Déduplication : évite les clips quasi identiques.

Deux moments sont doublons si l'un de ces critères est vrai :
  - recouvrement temporel (IoU) > seuil, ou l'un contient l'autre ;
  - transcriptions très similaires (même phrase répétée / replay) ;
  - images très proches (hash perceptuel dHash à 25 %, 50 % et 75 % du clip).
Les doublons qui se chevauchent sont fusionnés dans le meilleur moment si la durée
max le permet ; sinon ils sont marqués `duplicate_of`.
"""
from __future__ import annotations

import difflib

from app.core.config import Settings, get_settings
from app.scoring.lexicon import normalize
from app.video.analysis import AnalysisResult, hamming


def time_iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def contains(a: tuple[float, float], b: tuple[float, float], tol: float = 1.0) -> bool:
    return (a[0] - tol <= b[0] and b[1] <= a[1] + tol) or (b[0] - tol <= a[0] and a[1] <= b[1] + tol)


def transcript_similarity(a: str, b: str) -> float:
    ta, tb = normalize(a).split(), normalize(b).split()
    if len(ta) < 5 or len(tb) < 5:
        return 0.0
    return difflib.SequenceMatcher(None, ta, tb).ratio()


def phash_distance(analysis: AnalysisResult, a: tuple[float, float], b: tuple[float, float]) -> float | None:
    hashes = analysis.dhash
    if not hashes:
        return None

    def at(t: float) -> int | None:
        i = int(t)
        return int(hashes[i], 16) if 0 <= i < len(hashes) else None

    dists = []
    for frac in (0.25, 0.5, 0.75):
        ha, hb = at(a[0] + frac * (a[1] - a[0])), at(b[0] + frac * (b[1] - b[0]))
        if ha is None or hb is None:
            return None
        # une image uniforme (hash ~0) n'est pas informative
        if bin(ha).count("1") < 6 or bin(hb).count("1") < 6:
            return None
        dists.append(hamming(ha, hb))
    return sum(dists) / len(dists)


class DeduplicationService:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.iou_thr = float(self.s.get("dedup.time_iou_threshold", 0.35))
        self.txt_thr = float(self.s.get("dedup.transcript_similarity_threshold", 0.75))
        self.ph_thr = float(self.s.get("dedup.phash_distance_threshold", 10))
        self.max_d = float(self.s.get("video.max_clip_duration", 60))

    def is_duplicate(self, a, b, analysis: AnalysisResult | None) -> str | None:
        ra, rb = (a.start, a.end), (b.start, b.end)
        if time_iou(ra, rb) > self.iou_thr or contains(ra, rb):
            return "overlap"
        if transcript_similarity(a.transcript, b.transcript) > self.txt_thr:
            return "transcript"
        if analysis is not None:
            d = phash_distance(analysis, ra, rb)
            if d is not None and d <= self.ph_thr:
                return "visual"
        return None

    def filter_moments(self, ranked: list, analysis: AnalysisResult | None = None) -> list:
        """`ranked` trié par score décroissant. Retourne les moments uniques."""
        kept: list = []
        for idx, cand in enumerate(ranked):
            dup_of = None
            for k_idx, k in enumerate(kept):
                why = self.is_duplicate(k, cand, analysis)
                if why:
                    dup_of = k_idx
                    if why == "overlap":
                        # fusion : on étend le meilleur moment si ça reste dans la durée max
                        new_start, new_end = min(k.start, cand.start), max(k.end, cand.end)
                        if new_end - new_start <= self.max_d and cand.score >= k.score - 10:
                            k.start, k.end = new_start, new_end
                    cand.evidence = {**cand.evidence, "duplicate_reason": why}
                    break
            if dup_of is None:
                kept.append(cand)
            else:
                cand.duplicate_of = dup_of
        return kept
