"""HighlightDetectionService : détection et scoring des moments à potentiel viral.

Approche hybride, sans LLM obligatoire :
  1. signaux par pas de temps : audio (niveau, pic soudain, silence→explosion), vidéo
     (mouvement, changements de plan), texte (lexique de réactions, répétitions,
     exclamations, débit de parole, mots-clés de jeux) ;
  2. courbe d'activation lissée -> pics candidats ;
  3. pour chaque pic, composantes 0-1 (emotional_intensity, surprise, action_intensity,
     humor, context_independence, replayability, quotability, audio_intensity,
     visual_interest) -> viral_score pondéré (poids dans config.yaml) ;
  4. catégorie, raison lisible, confiance.
Le LLM (optionnel) affine ensuite les meilleurs candidats (voir captions/llm.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.core.config import Settings, get_settings
from app.scoring.games import GameDetector
from app.scoring.lexicon import EMOTION_CATEGORIES, find_hits, normalize
from app.transcription.base import Transcript
from app.video.analysis import AnalysisResult

COMPONENTS = ("emotional_intensity", "surprise", "action_intensity", "humor", "context_independence",
              "replayability", "quotability", "audio_intensity", "visual_interest")

CATEGORY_LABELS_FR = {
    "unexpected_reaction": "réaction inattendue",
    "funny": "moment drôle",
    "rage": "rage / colère",
    "victory_clutch": "victoire / clutch",
    "fail": "fail / défaite",
    "action": "action spectaculaire",
    "quote": "phrase marquante",
    "rare_event": "événement rare",
    "tension_release": "montée de tension puis explosion",
}


@dataclass
class MomentCandidate:
    peak: float
    onset: float
    reaction_end: float
    components: dict[str, float]
    score: float
    category: str
    reason: str
    confidence: float
    evidence: dict = field(default_factory=dict)
    start: float = 0.0           # rempli par ClipSelectionService
    end: float = 0.0
    transcript: str = ""
    duplicate_of: int | None = None

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 2)


# --------------------------------------------------------------------------- utils
def robust01(x: np.ndarray, zmax: float = 3.0) -> np.ndarray:
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med))) * 1.4826 + 1e-6
    return np.clip((x - med) / (mad * zmax), 0.0, 1.0)


def smooth(x: np.ndarray, width: int) -> np.ndarray:
    if width <= 1 or x.size == 0:
        return x.astype(np.float32)
    k = np.ones(width, dtype=np.float32) / width
    return np.convolve(x, k, mode="same")


def gaussian_spread(n: int, idxs: list[int], weights: list[float], sigma: float) -> np.ndarray:
    out = np.zeros(n, dtype=np.float32)
    if n == 0:
        return out
    radius = int(3 * sigma) + 1
    kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / max(sigma, 1e-3)) ** 2)
    for i, w in zip(idxs, weights):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        out[lo:hi] += w * kernel[(lo - i + radius):(hi - i + radius)]  # plusieurs indices = signal plus fort
    return np.clip(out, 0, 1)


def find_peaks(a: np.ndarray, min_dist: int, min_height: float) -> list[int]:
    order = np.argsort(-a)
    taken = np.zeros(a.size, dtype=bool)
    peaks: list[int] = []
    for i in order:
        if a[i] < min_height:
            break
        lo, hi = max(0, i - min_dist), min(a.size, i + min_dist + 1)
        if taken[lo:hi].any():
            continue
        peaks.append(int(i))
        taken[i] = True
    return peaks


# --------------------------------------------------------------------------- service
class HighlightDetectionService:
    def __init__(self, settings: Settings | None = None, game_detector: GameDetector | None = None):
        self.s = settings or get_settings()
        self.games = game_detector or GameDetector(self.s)

    # -- signaux ---------------------------------------------------------
    def build_signals(self, analysis: AnalysisResult, transcript: Transcript) -> dict[str, np.ndarray]:
        step = analysis.step
        n = len(analysis.audio_db)
        db = analysis.arr("audio_db")
        sig: dict[str, np.ndarray] = {}

        speech_floor = db > -60
        sig["audio"] = robust01(db) * speech_floor
        # pic soudain : niveau actuel vs moyenne des 8 s précédentes
        base_w = int(8 / step)
        gap = int(1.5 / step)
        spike = np.zeros(n, dtype=np.float32)
        calm = np.zeros(n, dtype=np.float32)
        med_db = float(np.median(db))
        for i in range(n):
            lo, hi = max(0, i - base_w), max(0, i - gap)
            if hi - lo >= 2:
                baseline = float(np.mean(db[lo:hi]))
                spike[i] = np.clip((db[i] - baseline) / 15.0, 0, 1)
                # silence (ou passage calme) suivi d'une explosion
                if baseline < med_db - 6 and db[i] - baseline > 12:
                    calm[i] = 1.0
        sig["spike"] = smooth(spike, 3)
        sig["silence_burst"] = smooth(calm, 3)

        motion = analysis.arr("motion")
        sig["motion"] = robust01(motion)
        cuts = analysis.arr("scene_cut")
        sig["scene_density"] = np.clip(smooth(cuts, int(4 / step)) * int(4 / step) / 3.0, 0, 1)
        sig["motion_var"] = np.clip(smooth(np.abs(np.diff(motion, prepend=motion[:1])), 4) / (motion.std() + 1e-3), 0, 1)

        words = transcript.words
        hits = find_hits(words)
        for cat in ("surprise", "humor", "anger", "victory", "defeat", "action", "rare"):
            cat_hits = [h for h in hits if h.category == cat]
            sig[f"text_{cat}"] = gaussian_spread(
                n, [min(n - 1, int(h.time / step)) for h in cat_hits], [0.8] * len(cat_hits), sigma=1.5 / step)
        # exclamations / répétitions / débit
        excl_idx, rep_idx = [], []
        norm_tokens = [normalize(w.text).strip(" .,!?…") for w in words]
        for k, w in enumerate(words):
            if "!" in w.text or "?!" in w.text:
                excl_idx.append(min(n - 1, int(w.start / step)))
            if k >= 2 and norm_tokens[k] and norm_tokens[k] == norm_tokens[k - 1] == norm_tokens[k - 2]:
                rep_idx.append(min(n - 1, int(w.start / step)))
        sig["text_excl"] = gaussian_spread(n, excl_idx, [0.7] * len(excl_idx), sigma=1.5 / step)
        sig["text_repeat"] = gaussian_spread(n, rep_idx, [1.0] * len(rep_idx), sigma=2 / step)
        rate = np.zeros(n, dtype=np.float32)
        for w in words:
            rate[min(n - 1, int(w.start / step))] += 1
        rate = smooth(rate, int(3 / step)) / step  # mots / seconde
        sig["speech_rate"] = robust01(rate) * (rate > 0)
        game_hits = self.games.hits(words)
        sig["text_game"] = gaussian_spread(
            n, [min(n - 1, int(h.time / step)) for h in game_hits], [0.6] * len(game_hits), sigma=3 / step)
        sig["text_emotion"] = np.max(np.stack([sig[f"text_{c}"] for c in EMOTION_CATEGORIES]), axis=0)

        sig["activation"] = smooth(
            0.30 * sig["audio"] + 0.30 * sig["spike"] + 0.25 * sig["silence_burst"] + 0.20 * sig["motion"]
            + 0.10 * sig["scene_density"] + 0.35 * sig["text_emotion"] + 0.10 * sig["text_excl"]
            + 0.15 * sig["text_repeat"] + 0.10 * sig["text_action"] + 0.10 * sig["text_rare"],
            int(2 / step),
        )
        return sig

    # -- détection -------------------------------------------------------
    def detect(self, analysis: AnalysisResult, transcript: Transcript, max_candidates: int | None = None,
               aggressiveness: str | None = None) -> list[MomentCandidate]:
        step = analysis.step
        sig = self.build_signals(analysis, transcript)
        a = sig["activation"]
        n = a.size
        if n < int(4 / step):
            return []
        aggr = aggressiveness or self.s.get("scoring.aggressiveness", "normal")
        wanted = int(self.s.get("video.clips_per_video", 10))
        max_candidates = max_candidates or max(wanted * 3, 10)
        min_dist = int(float(self.s.get("scoring.peak_min_distance", 20)) / step)
        pct = {"conservative": 75, "normal": 60, "aggressive": 40}.get(aggr, 60)
        min_height = max(float(np.percentile(a, pct)), 0.05)
        peaks = find_peaks(a, min_dist, min_height)[:max_candidates]

        mean_prob = float(np.mean([w.prob for w in transcript.words])) if transcript.words else 0.0
        candidates = [self._score_peak(p, sig, analysis, transcript, mean_prob) for p in peaks]
        threshold = float(self.s.get(f"scoring.min_score.{aggr}", 40))
        candidates = [c for c in candidates if c.score >= threshold]
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    def _score_peak(self, p: int, sig: dict, analysis: AnalysisResult, transcript: Transcript,
                    mean_prob: float) -> MomentCandidate:
        step = analysis.step
        n = sig["activation"].size
        a = sig["activation"]
        # début de la montée et fin de la réaction
        base = float(np.median(a))
        level = base + 0.35 * (a[p] - base)
        on = p
        while on > 0 and a[on - 1] > level and (p - on) * step < 12:
            on -= 1
        off = p
        while off < n - 1 and a[off + 1] > level and (off - p) * step < 15:
            off += 1
        lo, hi = max(0, on - int(2 / step)), min(n, off + int(2 / step) + 1)
        w = slice(lo, hi)

        def peak(name: str) -> float:
            return float(np.max(sig[name][w])) if hi > lo else 0.0

        def top_mean(name: str, frac: float = 0.3) -> float:
            seg = np.sort(sig[name][w])[::-1]
            k = max(1, int(len(seg) * frac))
            return float(seg[:k].mean()) if seg.size else 0.0

        audio_i = top_mean("audio")
        emo_text = peak("text_emotion")
        comps = {
            "audio_intensity": audio_i,
            "emotional_intensity": 0.55 * peak("audio") + 0.45 * emo_text + 0.15 * peak("text_excl"),
            "surprise": 0.6 * peak("spike") + 0.5 * peak("text_surprise") + 0.25 * peak("silence_burst"),
            "action_intensity": 0.7 * top_mean("motion") + 0.4 * peak("scene_density") + 0.3 * peak("text_action"),
            "humor": peak("text_humor") + 0.15 * peak("audio") * peak("text_humor"),
            "visual_interest": 0.6 * peak("scene_density") + 0.4 * top_mean("motion_var") + 0.2 * top_mean("motion"),
        }
        words_win = transcript.words_between(on * step - 6, off * step + 2)
        pause_before = any(
            b.start - a_.end >= 0.5 for a_, b in zip(words_win, words_win[1:]) if b.start <= on * step
        ) or not transcript.words_between(on * step - 6, on * step)
        comps["context_independence"] = 0.35 + 0.3 * float(pause_before) + 0.25 * min(1.0, emo_text + peak("text_action")) \
            + 0.1 * float(len(words_win) >= 6)
        comps["replayability"] = 0.45 * comps["surprise"] + 0.45 * comps["action_intensity"] + 0.4 * peak("text_rare")
        comps["quotability"] = 0.5 * peak("text_repeat") + 0.35 * peak("text_excl") + 0.35 * emo_text * peak("speech_rate")
        comps = {k: round(float(np.clip(v, 0, 1)), 3) for k, v in comps.items()}

        score = self.compute_score(comps)
        game_boost = peak("text_game")
        score = min(100.0, score + 4.0 * game_boost)

        cat_scores = {
            "unexpected_reaction": 0.6 * comps["surprise"] + 0.4 * comps["emotional_intensity"],
            "funny": comps["humor"] * 1.05,
            "rage": 0.75 * peak("text_anger") + 0.25 * audio_i,
            "victory_clutch": 0.75 * peak("text_victory") + 0.25 * comps["action_intensity"],
            "fail": 0.75 * peak("text_defeat") + 0.25 * comps["surprise"],
            "action": 0.7 * comps["action_intensity"],
            "quote": 0.8 * comps["quotability"],
            "rare_event": 0.8 * peak("text_rare") + 0.2 * comps["replayability"],
            "tension_release": 0.9 * peak("silence_burst"),
        }
        category = max(cat_scores, key=cat_scores.get)
        # Une preuve verbale explicite (« game over », « let's go », « mdr »…) l'emporte sur l'intensité générique.
        specific = {"funny": peak("text_humor"), "rage": peak("text_anger"), "victory_clutch": peak("text_victory"),
                    "fail": peak("text_defeat")}
        best_specific = max(specific, key=specific.get)
        if specific[best_specific] >= 0.75 and specific[best_specific] >= peak("text_surprise"):
            category = best_specific

        modalities = int(audio_i > 0.45 or peak("spike") > 0.5) + int(emo_text > 0.4) + \
            int(comps["action_intensity"] > 0.5 or comps["visual_interest"] > 0.5)
        conf = 0.45 + 0.15 * modalities + (0.1 * mean_prob if transcript.words else 0.0)
        conf = min(0.97 if transcript.words else 0.75, conf)

        quote = self._strongest_phrase(transcript, on * step - 1, off * step + 1)
        evidence = {
            "spike_db": round(float(peak("spike")) * 15, 1),
            "silence_burst": peak("silence_burst") > 0.5,
            "keywords": quote,
            "game_mention": game_boost > 0.3,
            "scene_cuts": int(np.sum(analysis.arr("scene_cut")[w])),
        }
        reason = self.explain(category, comps, evidence)
        return MomentCandidate(
            peak=round(p * step, 2), onset=round(on * step, 2), reaction_end=round(off * step, 2),
            components=comps, score=round(score, 1), category=category, reason=reason,
            confidence=round(conf, 2), evidence=evidence,
        )

    def compute_score(self, comps: dict[str, float]) -> float:
        weights = self.s.get("scoring.weights", {}) or {}
        total_w = sum(float(weights.get(k, 1.0)) for k in COMPONENTS)
        raw = sum(float(weights.get(k, 1.0)) * comps.get(k, 0.0) for k in COMPONENTS) / max(total_w, 1e-6)
        top3 = float(np.mean(sorted(comps.values(), reverse=True)[:3]))
        return round(100.0 * float(np.clip(0.55 * raw + 0.45 * top3, 0, 1)), 1)

    @staticmethod
    def _strongest_phrase(transcript: Transcript, start: float, end: float) -> list[str]:
        words = transcript.words_between(start, end)
        hits = [h for h in find_hits(words) if h.category in EMOTION_CATEGORIES + ("action", "rare")]
        seen, out = set(), []
        for h in hits:
            if h.phrase not in seen:
                seen.add(h.phrase)
                out.append(h.phrase)
        return out[:4]

    @staticmethod
    def explain(category: str, comps: dict, evidence: dict) -> str:
        label = CATEGORY_LABELS_FR.get(category, category)
        parts = []
        if evidence.get("silence_burst"):
            parts.append("explosion sonore juste après un passage calme")
        elif evidence.get("spike_db", 0) >= 6:
            parts.append(f"pic sonore soudain (~+{evidence['spike_db']:.0f} dB)")
        if evidence.get("keywords"):
            quoted = ", ".join(f"« {k} »" for k in evidence["keywords"][:3])
            parts.append(f"réaction verbale {quoted}")
        if comps.get("action_intensity", 0) >= 0.5:
            parts.append("forte action à l'écran")
        if evidence.get("scene_cuts", 0) >= 2:
            parts.append(f"{evidence['scene_cuts']} changements de plan")
        if comps.get("humor", 0) >= 0.5:
            parts.append("rires détectés")
        if evidence.get("game_mention"):
            parts.append("jeu populaire mentionné")
        if not parts:
            parts.append("intensité globale au-dessus de la moyenne de la vidéo")
        return f"{label.capitalize()} : " + " ; ".join(parts) + "."
