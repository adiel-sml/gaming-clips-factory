"""ClipSelectionService : bornes intelligentes (contexte avant / conclusion après) + top N.

Règles :
  - on remonte de 3 à 10 s avant le début de la montée d'intensité (onset), en
    s'arrêtant sur un début naturel : début de phrase, pause de parole, ou
    changement de plan ;
  - on termine 2 à 8 s après la fin de la réaction, sur une fin de phrase/pause ;
  - jamais de coupe au milieu d'un mot ;
  - durée forcée dans [min_clip_duration, max_clip_duration].
"""
from __future__ import annotations

from app.clips.dedup import DeduplicationService
from app.core.config import Settings, get_settings
from app.scoring.highlight import MomentCandidate
from app.transcription.base import Transcript, Word
from app.video.analysis import AnalysisResult

PAUSE = 0.55
SENTENCE_END = (".", "!", "?", "…")


class ClipSelectionService:
    def __init__(self, settings: Settings | None = None, dedup: DeduplicationService | None = None):
        self.s = settings or get_settings()
        self.dedup = dedup or DeduplicationService(self.s)

    # -------------------------------------------------------------- frontières
    @staticmethod
    def natural_starts(words: list[Word], cuts: list[float]) -> list[float]:
        """Instants où une séquence peut commencer naturellement."""
        starts = []
        for i, w in enumerate(words):
            if i == 0:
                starts.append(w.start)
                continue
            prev = words[i - 1]
            if w.start - prev.end >= PAUSE or prev.text.strip().endswith(SENTENCE_END):
                starts.append(w.start)
        return sorted(set(starts + cuts))

    @staticmethod
    def natural_ends(words: list[Word]) -> list[float]:
        ends = []
        for i, w in enumerate(words):
            nxt = words[i + 1] if i + 1 < len(words) else None
            if nxt is None or nxt.start - w.end >= PAUSE or w.text.strip().endswith(SENTENCE_END):
                ends.append(w.end)
        return ends

    @staticmethod
    def snap_out_of_words(t: float, words: list[Word], side: str) -> float:
        for w in words:
            if w.start < t < w.end:
                return max(0.0, w.start - 0.08) if side == "start" else w.end + 0.15
        return t

    def compute_bounds(self, c: MomentCandidate, transcript: Transcript, analysis: AnalysisResult) -> tuple[float, float]:
        dur_total = analysis.duration
        min_d = float(self.s.get("video.min_clip_duration", 15))
        max_d = float(self.s.get("video.max_clip_duration", 60))
        cb_min, cb_max = [float(x) for x in self.s.get("scoring.context_before", [3, 10])]
        ca_min, ca_max = [float(x) for x in self.s.get("scoring.context_after", [2, 8])]
        words = transcript.words
        cuts = [i * analysis.step for i, v in enumerate(analysis.scene_cut) if v]

        # ---- début : le début naturel le plus tardif qui laisse au moins cb_min de contexte
        lo, hi = c.onset - cb_max, c.onset - cb_min
        starts = [t for t in self.natural_starts(words, cuts) if lo <= t <= hi]
        if starts:
            start = max(starts)
        else:
            # pas de début naturel : si quelqu'un parle avant, on garde plus de contexte
            speech_before = transcript.words_between(lo, c.onset)
            start = c.onset - (min(cb_max, cb_min + 3) if speech_before else cb_min + 1)

        # ---- fin : la première fin naturelle après la réaction (+ ca_min)
        lo_e, hi_e = c.reaction_end + ca_min, c.reaction_end + ca_max
        ends = [t for t in self.natural_ends(words) if lo_e <= t <= hi_e]
        end = min(ends) + 0.25 if ends else c.reaction_end + min(ca_max, ca_min + 2)

        start, end = max(0.0, start), min(dur_total, end)

        # ---- contraintes de durée
        if end - start < min_d:
            missing = min_d - (end - start)
            extra_end = min(missing * 0.4, dur_total - end)
            end += extra_end
            missing -= extra_end
            earlier = [t for t in self.natural_starts(words, cuts) if start - missing - 6 <= t <= start - missing]
            start = max(0.0, max(earlier) if earlier else start - missing)
            if end - start < min_d:  # bord de vidéo : on prolonge de l'autre côté
                end = min(dur_total, start + min_d)
                start = max(0.0, end - min_d)
        if end - start > max_d:
            start = max(start, c.onset - cb_min)
            if end - start > max_d:
                ends_ok = [t for t in self.natural_ends(words) if start + min_d <= t <= start + max_d]
                end = max(ends_ok) + 0.2 if ends_ok else start + max_d

        start = self.snap_out_of_words(start, words, "start")
        end = self.snap_out_of_words(end, words, "end")
        end = min(end, dur_total, start + max_d)
        return round(max(0.0, start), 2), round(end, 2)

    # -------------------------------------------------------------- sélection
    def select(self, candidates: list[MomentCandidate], transcript: Transcript, analysis: AnalysisResult,
               top_n: int | None = None) -> list[MomentCandidate]:
        top_n = top_n or int(self.s.get("video.clips_per_video", 10))
        for c in candidates:
            c.start, c.end = self.compute_bounds(c, transcript, analysis)
            c.transcript = transcript.text_between(c.start, c.end)
        ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
        kept = self.dedup.filter_moments(ranked, analysis)
        return kept[:top_n]
