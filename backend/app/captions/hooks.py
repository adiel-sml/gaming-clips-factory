"""HookGenerationService : 1 à 3 premières secondes.

Règle d'or : ne jamais promettre un événement absent du clip. Les modèles de hook
sont choisis selon la catégorie *mesurée* du moment (réaction, fail, clutch…) et
peuvent citer une phrase réellement prononcée. Les hooks issus du LLM sont validés
(aucun nom propre ni chiffre absent de la transcription).
"""
from __future__ import annotations

import re

from app.captions.llm import LLMProvider, NullLLM
from app.core.config import Settings, get_settings
from app.scoring.lexicon import EMOTION_CATEGORIES, find_hits, normalize
from app.transcription.base import Word

TEMPLATES = {
    "fr": {
        "unexpected_reaction": ["IL NE S'ATTENDAIT PAS À ÇA 💀", "ATTENDS DE VOIR SA RÉACTION…", "PERSONNE N'ÉTAIT PRÊT 😳"],
        "funny": ["J'AI PLEURÉ DE RIRE 😭", "IMPOSSIBLE DE RESTER SÉRIEUX 😂", "REGARDE JUSQU'À LA FIN 😭"],
        "rage": ["IL A COMPLÈTEMENT CRAQUÉ 😤", "LA RAGE EST RÉELLE 💢", "ÇA L'A RENDU FOU…"],
        "victory_clutch": ["LE CLUTCH LE PLUS PROPRE 🔥", "IL A RENVERSÉ LA SITUATION 😮", "PERSONNE N'Y CROYAIT…"],
        "fail": ["ÇA S'EST MAL TERMINÉ 💀", "LE FAIL DE L'ANNÉE 😭", "IL ÉTAIT SI PROCHE…"],
        "action": ["ÇA DÉGÉNÈRE TRÈS VITE 🔥", "REGARDE CE QUI SE PASSE 😳", "L'ACTION LA PLUS FOLLE"],
        "quote": ["CETTE PHRASE… 💀", "IL A VRAIMENT DIT ÇA 😭", "ÉCOUTE BIEN 👂"],
        "rare_event": ["ÇA N'ARRIVE JAMAIS 😳", "TU N'AS JAMAIS VU ÇA", "MOMENT ULTRA RARE 👀"],
        "tension_release": ["LE CALME AVANT LA TEMPÊTE…", "ATTENDS 3 SECONDES… 💀", "TOUT A BASCULÉ D'UN COUP"],
    },
    "en": {
        "unexpected_reaction": ["HE DID NOT SEE THAT COMING 💀", "WAIT FOR HIS REACTION…", "NOBODY WAS READY 😳"],
        "funny": ["I CRIED LAUGHING 😭", "TRY NOT TO LAUGH 😂", "WATCH TILL THE END 😭"],
        "rage": ["HE COMPLETELY LOST IT 😤", "THE RAGE IS REAL 💢", "THIS BROKE HIM…"],
        "victory_clutch": ["CLEANEST CLUTCH EVER 🔥", "HE TURNED IT AROUND 😮", "NOBODY BELIEVED IT…"],
        "fail": ["THIS ENDED BADLY 💀", "FAIL OF THE YEAR 😭", "SO CLOSE…"],
        "action": ["THIS ESCALATED FAST 🔥", "LOOK WHAT HAPPENS 😳", "CRAZIEST ACTION"],
        "quote": ["THIS LINE… 💀", "HE REALLY SAID THAT 😭", "LISTEN CLOSELY 👂"],
        "rare_event": ["THIS NEVER HAPPENS 😳", "YOU'VE NEVER SEEN THIS", "ULTRA RARE MOMENT 👀"],
        "tension_release": ["THE CALM BEFORE THE STORM…", "WAIT 3 SECONDS… 💀", "EVERYTHING FLIPPED"],
    },
}

HOOK_SCHEMA = {
    "type": "object",
    "properties": {"hooks": {"type": "array", "items": {"type": "string"}}},
    "required": ["hooks"],
    "additionalProperties": False,
}


def _lang(language: str | None) -> str:
    return "en" if (language or "").startswith("en") else "fr"


def quote_hook(words: list[Word], max_words: int = 6) -> str | None:
    """Courte phrase réellement prononcée, contenant une réaction émotionnelle."""
    if not words:
        return None
    sentences: list[list[Word]] = [[]]
    for w in words:
        sentences[-1].append(w)
        if w.text.strip().endswith((".", "!", "?", "…")):
            sentences.append([])
    best = None
    for sent in sentences:
        if not sent or len(sent) > max_words:
            continue
        if any(h.category in EMOTION_CATEGORIES for h in find_hits(sent)):
            text = " ".join(w.text.strip() for w in sent).strip(" .")
            if best is None or len(text) < len(best):
                best = text
    return f"« {best.upper()} »" if best else None


def is_grounded(hook: str, transcript: str, allowed_terms: list[str]) -> bool:
    """Refuse un hook qui mentionne des chiffres ou noms propres absents de la transcription."""
    ref = normalize(transcript + " " + " ".join(allowed_terms))
    for token in re.findall(r"[A-Za-zÀ-ÿ0-9']+", hook):
        if any(c.isdigit() for c in token) and normalize(token) not in ref:
            if token not in ("3",):  # « attends 3 secondes » est un procédé de rythme, pas un fait
                return False
        if token[:1].isupper() and token[1:].islower() and len(token) > 3 and normalize(token) not in ref:
            return False
    return True


class HookGenerationService:
    def __init__(self, settings: Settings | None = None, llm: LLMProvider | None = None):
        self.s = settings or get_settings()
        self.llm = llm or NullLLM()

    def generate(self, category: str, words: list[Word], transcript: str, language: str | None = None,
                 game_name: str | None = None, reason: str = "") -> list[dict]:
        lang = _lang(language)
        candidates: list[tuple[str, str]] = []
        if self.llm.available and transcript:
            data = self.llm.generate_json(
                "Tu écris des hooks de 2-3 secondes pour des clips gaming verticaux. Majuscules, max 7 mots, "
                "un emoji maximum. Le hook doit refléter UNIQUEMENT ce qui se passe réellement dans la "
                "transcription fournie. N'invente aucun événement, nom, ou chiffre.",
                f"Langue : {lang}\nCatégorie détectée : {category}\nJeu : {game_name or 'inconnu'}\n"
                f"Analyse : {reason}\nTranscription du clip :\n{transcript}\n\nPropose 3 hooks.",
                HOOK_SCHEMA, max_tokens=1500,
            )
            allowed = [game_name] if game_name else []
            for h in (data or {}).get("hooks", []):
                if h and is_grounded(h, transcript, allowed):
                    candidates.append((h.strip().upper(), "llm"))
        q = quote_hook(words)
        if q:
            candidates.append((q, "quote"))
        candidates += [(t, "template") for t in TEMPLATES[lang].get(category, TEMPLATES[lang]["unexpected_reaction"])]
        seen, unique = set(), []
        for text, source in candidates:
            if text not in seen:
                seen.add(text)
                unique.append((text, source))
        dur = float(self.s.get("hooks.duration", 2.5))
        return [{"text": t, "duration": dur, "position": self.s.get("hooks.position", "top"),
                 "style": self.s.get("hooks.style", "default"), "source": src}
                for t, src in unique[:3]]
