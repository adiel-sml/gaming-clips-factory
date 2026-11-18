"""Détection du jeu (GTA 6, Fortnite…) à partir du titre et de la transcription. Config : config/games.json."""
from __future__ import annotations

import re
from collections import Counter

from app.core.config import Settings, get_settings
from app.scoring.lexicon import compile_keywords, find_hits, normalize


class GameDetector:
    def __init__(self, settings: Settings | None = None, games: dict | None = None):
        self.games = games if games is not None else (settings or get_settings()).games()
        self._patterns = {gid: compile_keywords(g.get("keywords", []) + g.get("related", []))
                          for gid, g in self.games.items()}

    def detect_text(self, text: str) -> Counter:
        norm = normalize(text or "")
        counts: Counter = Counter()
        for gid, pats in self._patterns.items():
            for p in pats:
                counts[gid] += len(p.findall(norm))
        return +counts

    def detect(self, title: str | None, words: list) -> str | None:
        counts = Counter()
        for gid, n in self.detect_text(re.sub(r"[_\-.]+", " ", title or "")).items():
            counts[gid] += 5 * n                       # le titre pèse plus lourd
        for hit in find_hits(words, self._patterns):
            counts[hit.category] += 1
        if not counts:
            return None
        gid, n = counts.most_common(1)[0]
        return gid if n >= 2 else None

    def hits(self, words: list):
        return find_hits(words, self._patterns)

    def hashtags(self, game_id: str | None) -> list[str]:
        if not game_id or game_id not in self.games:
            return []
        return list(self.games[game_id].get("hashtags", []))

    def name(self, game_id: str | None) -> str | None:
        if not game_id or game_id not in self.games:
            return None
        return self.games[game_id].get("name", game_id)
