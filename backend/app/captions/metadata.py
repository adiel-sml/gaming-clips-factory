"""MetadataGenerationService : 3 titres, 3 captions, 5 à 10 hashtags liés au contenu réel."""
from __future__ import annotations

from app.captions.llm import LLMProvider, NullLLM
from app.core.config import Settings, get_settings
from app.scoring.games import GameDetector

CATEGORY_TAGS = {
    "unexpected_reaction": ["#reaction", "#streamerreaction"],
    "funny": ["#funny", "#fyp"],
    "rage": ["#rage", "#ragequit"],
    "victory_clutch": ["#clutch", "#win"],
    "fail": ["#fail", "#gamingfails"],
    "action": ["#epic", "#gameplay"],
    "quote": ["#streamer", "#quote"],
    "rare_event": ["#rare", "#gamingmoments"],
    "tension_release": ["#intense", "#gamingmoments"],
}

TITLES = {
    "fr": {
        "default": ["{hook_clean}", "{game_prefix}Ce moment est complètement fou", "{game_prefix}Personne ne s'attendait à ça"],
        "funny": ["{hook_clean}", "{game_prefix}Le moment le plus drôle du stream", "{game_prefix}J'en peux plus 😭"],
        "fail": ["{hook_clean}", "{game_prefix}Le fail le plus douloureux", "{game_prefix}Il y était presque…"],
        "victory_clutch": ["{hook_clean}", "{game_prefix}Le clutch impossible", "{game_prefix}Personne n'y croyait"],
    },
    "en": {
        "default": ["{hook_clean}", "{game_prefix}This moment is insane", "{game_prefix}Nobody expected this"],
        "funny": ["{hook_clean}", "{game_prefix}Funniest moment of the stream", "{game_prefix}I can't 😭"],
        "fail": ["{hook_clean}", "{game_prefix}Most painful fail", "{game_prefix}So close…"],
        "victory_clutch": ["{hook_clean}", "{game_prefix}The impossible clutch", "{game_prefix}Nobody believed it"],
    },
}

CAPTIONS = {
    "fr": ["{title} {tags}", "Sa réaction à la fin 😭 {tags}", "Tu aurais fait quoi à sa place ? 👇 {tags}"],
    "en": ["{title} {tags}", "His reaction at the end 😭 {tags}", "What would you have done? 👇 {tags}"],
}

COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"}},
        "captions": {"type": "array", "items": {"type": "string"}},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titles", "captions", "hashtags"],
    "additionalProperties": False,
}


def _clean_tag(tag: str) -> str:
    tag = "#" + "".join(c for c in tag.lower().lstrip("#") if c.isalnum() or c == "_")
    return tag if len(tag) > 2 else ""


class MetadataGenerationService:
    def __init__(self, settings: Settings | None = None, llm: LLMProvider | None = None,
                 games: GameDetector | None = None):
        self.s = settings or get_settings()
        self.llm = llm or NullLLM()
        self.games = games or GameDetector(self.s)

    def hashtags(self, category: str, game_id: str | None, language: str | None, source_type: str = "stream",
                 extra: list[str] | None = None) -> list[str]:
        tags = ["#gaming", "#gamingclips"]
        tags += self.games.hashtags(game_id)[:3]
        tags += CATEGORY_TAGS.get(category, [])
        tags += extra or []
        if source_type == "stream":
            tags.append("#twitchclips")
        if (language or "").startswith("fr"):
            tags.append("#gamingfr")
        out = []
        for t in tags:
            t = _clean_tag(t)
            if t and t not in out:
                out.append(t)
        return out[:10]

    def generate(self, category: str, transcript: str, hook_text: str, game_id: str | None,
                 language: str | None, reason: str = "") -> dict:
        lang = "en" if (language or "").startswith("en") else "fr"
        game_name = self.games.name(game_id)
        tags = self.hashtags(category, game_id, language)
        hook_clean = hook_text.replace("«", "").replace("»", "").strip().capitalize() if hook_text else ""
        game_prefix = f"{game_name} : " if game_name and lang == "fr" else (f"{game_name}: " if game_name else "")
        tpl = TITLES[lang].get(category, TITLES[lang]["default"])
        titles = [t.format(hook_clean=hook_clean or tpl[1].format(game_prefix=game_prefix, hook_clean=""),
                           game_prefix=game_prefix).strip() for t in tpl]
        tag_str = " ".join(tags[:5])
        captions = [c.format(title=titles[0], tags=tag_str) for c in CAPTIONS[lang]]
        result = {"titles": titles, "captions": captions, "hashtags": tags, "source": "rules"}

        if self.llm.available and transcript:
            data = self.llm.generate_json(
                "Tu rédiges les métadonnées de clips gaming courts. Titres accrocheurs (< 70 caractères), "
                "captions courtes, hashtags pertinents uniquement (5 à 10, pas de spam). Ne décris que ce qui "
                "est réellement dans la transcription. N'invente aucun fait.",
                f"Langue : {lang}\nJeu : {game_name or 'inconnu'}\nCatégorie : {category}\nAnalyse : {reason}\n"
                f"Hook choisi : {hook_text}\nTranscription :\n{transcript}\n\n"
                "Donne exactement 3 titres, 3 captions et 5 à 10 hashtags.",
                COPY_SCHEMA, max_tokens=2000,
            )
            if data and len(data.get("titles", [])) >= 3:
                llm_tags = [_clean_tag(t) for t in data.get("hashtags", [])]
                merged = []
                for t in tags[:4] + llm_tags:  # on garde les hashtags « sûrs » (jeu, gaming) en tête
                    if t and t not in merged:
                        merged.append(t)
                result = {"titles": data["titles"][:3], "captions": data.get("captions", captions)[:3],
                          "hashtags": merged[:10], "source": self.llm.name}
        return result
