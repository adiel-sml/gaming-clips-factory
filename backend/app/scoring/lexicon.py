"""Lexique multilingue (FR/EN) de réactions de streamers.

Le texte est normalisé (minuscules, sans accents, lettres répétées réduites à 2)
avant la recherche : « NOOOOON » -> « noon », « ahahahah » -> « ahahahah ».
Les motifs sont des regex sur ce texte normalisé.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass

LEXICON: dict[str, list[str]] = {
    "surprise": [
        r"\bquoi+\b", r"\bwhat\b", r"c'?est pas possible", r"\bpas possible\b", r"j'?y crois pas",
        r"\bno way\b", r"oh mon dieu", r"\bomg\b", r"oh my god", r"\bwtf\b", r"\battends?\b", r"\bwait\b",
        r"\bserieux\b", r"\bincroyable\b", r"\binsane\b", r"\bdingue\b", r"\bc'?est fou\b", r"\bcrazy\b",
        r"\bhein\b", r"\bnan mais\b", r"\boh+ la la\b", r"\bnon mais\b", r"\bc'?est quoi (ca|ce)\b",
        r"\bholy\b", r"\bjamais\b", r"\bimpossible\b", r"\bje reve\b",
    ],
    "humor": [
        r"\b(a?ha){2,}h?\b", r"\b(ha[ ,]+){2,}ha\b", r"\b(he){2,}\b", r"\bmdr+\b", r"\bptdr+\b", r"\blol\b", r"\blmao\b", r"\bjpp\b",
        r"\btrop drole\b", r"\bfunny\b", r"\bhilarant\b", r"\bje suis plie\b", r"\bje rigole\b", r"\bmort de rire\b",
        r"\bc'?est (trop )?marrant\b",
    ],
    "anger": [
        r"\bnon non\b", r"\bmais non\b", r"\bno+n?\b(?= ?!)", r"\bnoo+n?\b", r"\barnaque\b", r"\bj'?en ai marre\b",
        r"\bbordel\b", r"\bputain\b", r"\bpourquoi\b", r"\bwhy\b", r"\bc'?est nul\b", r"\bgame de merde\b",
        r"\btriche\b", r"\bcheat\w*\b", r"\bscandale\b", r"\bfuck\w*\b", r"\bdamn\b",
    ],
    "victory": [
        r"\blet'?s go+\b", r"\bgo go\b", r"\bvictoire\b", r"\bon a gagne\b", r"\bgagne\b", r"\btop ?1\b",
        r"\bon l'?a\b", r"\bwe did it\b", r"\bclutch\b", r"\bace\b", r"\bgg\b", r"\byes+\b", r"\bouais+\b",
        r"\btrop fort\b", r"\bvictory\b", r"\bwin\b", r"\bje l'?ai eu\b", r"\bc'?est bon\b", r"\bincroyable action\b",
    ],
    "defeat": [
        r"\bje suis mort\b", r"\bon est mort\b", r"\bperdu\b", r"\bdead\b", r"\brate\b", r"\bfail\b", r"\blost\b",
        r"\bc'?est fini\b", r"\bgame over\b", r"\bil m'?a eu\b", r"\bil m'?a tue\b", r"\bje meurs\b", r"\bwasted\b",
    ],
    "action": [
        r"\bexplos\w*\b", r"\bboss\b", r"\bheadshot\b", r"\bkill\w*\b", r"\btue\b", r"\btriple\b", r"\bquadra\b",
        r"\bpenta\b", r"\bcrash\w*\b", r"\bpoursuite\b", r"\bpolice\b", r"\bflics?\b", r"\bcops\b", r"\bwanted\b",
        r"\bbraquage\b", r"\bheist\b", r"\bcourse\b", r"\bfonce\b", r"\btire\b", r"\bshoot\b",
    ],
    "rare": [
        r"\bjamais vu\b", r"\bnever seen\b", r"\bpremiere fois\b", r"\bfirst time\b", r"\brare\b", r"\bsecret\b",
        r"\beaster egg\b", r"\bbug\b", r"\bglitch\b", r"\bleak\w*\b", r"\btrailer\b", r"\bannonce\b",
    ],
}

_COMPILED = {cat: [re.compile(p) for p in pats] for cat, pats in LEXICON.items()}
EMOTION_CATEGORIES = ("surprise", "humor", "anger", "victory", "defeat")


def build_lexicon(extra: dict[str, list[str]] | None = None) -> dict[str, list[re.Pattern]]:
    """Lexique de base + vocabulaire propre au profil de contenu (sport, émission…)."""
    if not extra:
        return _COMPILED
    out = {cat: list(pats) for cat, pats in _COMPILED.items()}
    for cat, patterns in extra.items():
        out.setdefault(cat, []).extend(re.compile(p) for p in patterns)
    return out


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'")
    text = re.sub(r"([a-z])\1{2,}", r"\1\1", text)
    return text


@dataclass
class LexHit:
    category: str
    time: float
    phrase: str


def find_hits(words: list, categories: dict[str, list[re.Pattern]] | None = None) -> list[LexHit]:
    """Recherche les motifs sur la suite de mots (avec timestamps). `words` : objets avec .start/.text."""
    categories = categories or _COMPILED
    if not words:
        return []
    offsets: list[int] = []
    parts: list[str] = []
    pos = 0
    for w in words:
        token = normalize(w.text)
        offsets.append(pos)
        parts.append(token)
        pos += len(token) + 1
    joined = " ".join(parts)
    hits: list[LexHit] = []
    for cat, patterns in categories.items():
        for pat in patterns:
            for m in pat.finditer(joined):
                idx = max(0, bisect.bisect_right(offsets, m.start()) - 1)
                hits.append(LexHit(cat, float(words[idx].start), m.group(0)))
    return hits


def compile_keywords(keywords: list[str]) -> list[re.Pattern]:
    return [re.compile(r"(?<!\w)" + re.escape(normalize(k)).replace(r"\ ", r"\s*") + r"(?!\w)") for k in keywords]
