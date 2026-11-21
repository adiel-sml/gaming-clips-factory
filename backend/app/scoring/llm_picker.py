"""Choix des moments par un LLM (méthode « transcription annotée »).

1. On construit une transcription horodatée, annotée avec les signaux mesurés :
       [00:09:10] Non c'est pas possible !   <<FORT +12 dB>> <<ACTION À L'ÉCRAN>>
       [00:09:14] (pas de parole)            <<FORT +15 dB>> <<CALME → EXPLOSION>>
2. Le LLM lit des fenêtres de ~12 min et note chaque moment de 0 à 10, sévèrement :
   la plupart des fenêtres ne contiennent rien de publiable.
3. Seuls les moments ≥ seuil (7 par défaut) sont gardés, avec des bornes serrées
   (déclencheur → chute), une catégorie, un hook et une raison.
Sans clé API, le pipeline utilise les règles classiques (HighlightDetectionService).
"""
from __future__ import annotations

import json

import numpy as np

from app.captions.llm import LLMProvider
from app.core.utils import fmt_ts, get_logger, parse_ts
from app.scoring.highlight import HighlightDetectionService, MomentCandidate
from app.transcription.base import Transcript, join_words
from app.video.analysis import AnalysisResult

log = get_logger("llm_picker")

CATEGORIES = ["unexpected_reaction", "funny", "rage", "victory_clutch", "fail", "action", "quote",
              "rare_event", "tension_release"]

SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "HH:MM:SS, quelques secondes avant le déclencheur"},
                    "end": {"type": "string", "description": "HH:MM:SS, juste après la chute / la réaction"},
                    "score": {"type": "integer", "description": "0-10, sévère"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "hook": {"type": "string", "description": "accroche de 7 mots max, fidèle au contenu"},
                    "reason": {"type": "string", "description": "une phrase, en français, fondée sur la transcription"},
                },
                "required": ["start", "end", "score", "category", "hook", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}

SYSTEM = """Tu es un monteur expert en clips viraux (TikTok, YouTube Shorts, Reels).
Type de contenu : {profile}

Tu reçois une transcription horodatée, annotée avec des signaux mesurés automatiquement :
<<FORT +X dB>> pic sonore soudain · <<CALME → EXPLOSION>> silence puis explosion ·
<<ACTION À L'ÉCRAN>> beaucoup de mouvement · <<MONTAGE RAPIDE>> changements de plan · (pas de parole) son fort sans voix.

Ta mission : repérer les moments qui feraient VRAIMENT arrêter le scroll, et les noter de 0 à 10.
Sois sévère :
- 9-10 : exceptionnel, tout le monde le partagerait.
- 7-8 : très bon clip, fort et compréhensible seul.
- 5-6 : correct mais banal → ne le propose pas.
- La plupart des fenêtres ne contiennent AUCUN moment ≥ 7 : renvoie alors une liste vide.
Un mot fort isolé ne suffit pas : il faut une vraie situation (surprise, chute, réaction, action, punchline).

Bornes : commence 1 à 4 s avant le déclencheur (juste assez de contexte), termine dès la chute ou la
réaction terminée. Durée visée {target} s (entre {min_d} et {max_d} s). Pas d'introduction inutile.
N'invente rien : chaque moment, hook et raison doivent correspondre à ce qui est dans la transcription.
Horodatages au format HH:MM:SS, compris dans la fenêtre fournie."""


def _markers(sig: dict, step: float, start: float, end: float, n: int) -> list[str]:
    lo, hi = max(0, int(start / step)), min(n, int(end / step) + 1)
    if hi <= lo:
        return []
    out = []
    spike = float(np.max(sig["spike"][lo:hi])) * 15
    if float(np.max(sig["silence_burst"][lo:hi])) > 0.5:
        out.append("<<CALME → EXPLOSION>>")
    if spike >= 6:
        out.append(f"<<FORT +{spike:.0f} dB>>")
    if float(np.max(sig["motion"][lo:hi])) > 0.75:
        out.append("<<ACTION À L'ÉCRAN>>")
    if float(np.max(sig["scene_density"][lo:hi])) > 0.6:
        out.append("<<MONTAGE RAPIDE>>")
    return out


def build_annotated_transcript(transcript: Transcript, sig: dict, analysis: AnalysisResult) -> list[tuple[float, str]]:
    """Lignes (temps, texte annoté), parole + moments forts sans parole, triées par temps."""
    step, n = analysis.step, len(analysis.audio_db)
    lines: list[tuple[float, str]] = []
    covered = np.zeros(n, dtype=bool)
    for seg in transcript.segments:
        text = join_words([w.text for w in seg.words]) if seg.words else seg.text
        if not text.strip():
            continue
        marks = _markers(sig, step, seg.start, seg.end, n)
        lines.append((seg.start, f"[{fmt_ts(seg.start)[:8]}] {text.strip()}" + (" " + " ".join(marks) if marks else "")))
        covered[max(0, int(seg.start / step)):min(n, int(seg.end / step) + 1)] = True
    # moments forts sans parole (public qui explose, explosion dans le jeu…)
    loud = (sig["spike"] * 15 >= 9) | (sig["silence_burst"] > 0.5)
    i = 0
    while i < n:
        if loud[i] and not covered[i]:
            j = i
            while j + 1 < n and loud[j + 1] and not covered[j + 1]:
                j += 1
            t = i * step
            marks = _markers(sig, step, t, (j + 1) * step, n)
            lines.append((t, f"[{fmt_ts(t)[:8]}] (pas de parole) " + " ".join(marks)))
            i = j + 1
        else:
            i += 1
    lines.sort(key=lambda x: x[0])
    return lines


class LLMMomentPicker:
    def __init__(self, llm: LLMProvider, detector: HighlightDetectionService):
        self.llm = llm
        self.detector = detector
        self.s = detector.s

    def pick(self, analysis: AnalysisResult, transcript: Transcript, progress=None) -> list[MomentCandidate]:
        s = self.s
        sig = self.detector.build_signals(analysis, transcript)
        lines = build_annotated_transcript(transcript, sig, analysis)
        if not lines:
            return []
        window = float(s.get("scoring.llm_window", 720))
        overlap = 60.0
        min_score = float(s.get("scoring.llm_min_score", 7))
        profile = s.profile()
        system = SYSTEM.format(
            profile=profile.get("description", "contenu vidéo"),
            target=s.get("video.target_clip_duration", 25), min_d=s.get("video.min_clip_duration", 12),
            max_d=s.get("video.max_clip_duration", 45))
        picks: list[dict] = []
        t0, duration = 0.0, analysis.duration
        windows = max(1, int(np.ceil(duration / (window - overlap))))
        k = 0
        while t0 < duration:
            t1 = min(duration, t0 + window)
            chunk = [txt for t, txt in lines if t0 <= t < t1]
            k += 1
            if progress:
                progress(min(1.0, k / windows))
            if chunk:
                prompt = (f"Fenêtre {fmt_ts(t0)[:8]} → {fmt_ts(t1)[:8]}.\n\n" + "\n".join(chunk)
                          + "\n\nRenvoie les moments ≥ 7 de cette fenêtre (souvent aucun).")
                data = self.llm.generate_json(system, prompt, SCHEMA, max_tokens=4000)
                if data is None:
                    raise RuntimeError("le LLM n'a pas répondu")
                for m in data.get("moments", []):
                    try:
                        a, b = parse_ts(m["start"]), parse_ts(m["end"])
                    except (ValueError, KeyError):
                        continue
                    if b - a >= 3 and t0 - 5 <= a < t1 + 5 and int(m.get("score", 0)) >= min_score:
                        picks.append({**m, "a": max(0.0, a), "b": min(duration, b)})
            if t1 >= duration:
                break
            t0 = t1 - overlap
        log.info("LLM : %d moment(s) ≥ %.0f sur %d fenêtre(s)", len(picks), min_score, k)
        return [self._to_candidate(p, sig, analysis, transcript) for p in picks]

    def _to_candidate(self, p: dict, sig: dict, analysis: AnalysisResult, transcript: Transcript) -> MomentCandidate:
        step = analysis.step
        a, b = p["a"], p["b"]
        lo, hi = int(a / step), max(int(a / step) + 1, int(b / step))
        act = sig["activation"]
        peak_i = lo + int(np.argmax(act[lo:hi])) if hi <= act.size and hi > lo else min(lo, act.size - 1)
        mean_prob = float(np.mean([w.prob for w in transcript.words])) if transcript.words else 0.0
        rule = self.detector._score_peak(peak_i, sig, analysis, transcript, mean_prob)
        llm_score = int(p["score"])
        c = MomentCandidate(
            peak=rule.peak, onset=a, reaction_end=b, components=rule.components,
            score=round(min(100.0, 0.7 * llm_score * 10 + 0.3 * rule.score), 1),
            category=p.get("category", rule.category), reason=p.get("reason") or rule.reason,
            confidence=round(min(0.98, 0.5 + llm_score / 20), 2),
            evidence={**rule.evidence, "llm": True, "llm_score": llm_score, "llm_hook": p.get("hook", ""),
                      "rule_score": rule.score, "rule_reason": rule.reason},
        )
        c.start, c.end = a, b
        return c


def picks_to_json(cands: list[MomentCandidate]) -> str:  # utilitaire de débogage
    return json.dumps([{"start": c.start, "end": c.end, "score": c.score, "cat": c.category} for c in cands], ensure_ascii=False)
