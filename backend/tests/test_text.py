"""Lexique, jointure des mots, détection de jeu, hooks et métadonnées."""
from app.captions.hooks import HookGenerationService, is_grounded, quote_hook
from app.captions.metadata import MetadataGenerationService
from app.scoring.games import GameDetector
from app.scoring.lexicon import find_hits, normalize
from app.transcription.base import Word, join_words, merge_fragments

from .conftest import make_words


def test_normalize_reduces_repeats_and_accents():
    assert normalize("NOOOOON c'est Énorme") == "noon c'est enorme"


def test_find_hits_categories():
    words = make_words([(0, "NOOOON c'est pas possible ! Let's goooo mdr")])
    cats = {h.category for h in find_hits(words)}
    assert {"surprise", "anger", "victory", "humor"} <= cats
    hit = next(h for h in find_hits(words) if h.category == "surprise")
    assert 0 <= hit.time < 3


def test_join_words_and_merge_fragments():
    assert join_words(["l", "'eau", "regardez", "-le", "!"]) == "l'eau regardez-le!"
    merged = merge_fragments([Word(0, 0.3, "Aujourd"), Word(0.3, 0.5, "'hui"), Word(0.6, 0.8, "on"), Word(0.8, 0.9, "!")])
    assert [w.text for w in merged] == ["Aujourd'hui", "on!"]
    assert merged[0].start == 0 and merged[0].end == 0.5


def test_game_detection_title_and_transcript(settings):
    gd = GameDetector(settings)
    assert gd.detect("Mon stream GTA VI", []) == "gta6"
    assert gd.detect("test_stream_gta6", []) == "gta6"
    words = make_words([(0, "on joue à GTA 6 aujourd'hui, Vice City est magnifique")])
    assert gd.detect(None, words) == "gta6"
    assert gd.detect("Une vidéo de cuisine", make_words([(0, "on coupe des carottes")])) is None
    assert "#gta6" in gd.hashtags("gta6")


def test_game_config_is_extensible(settings):
    gd = GameDetector(settings, games={"rl": {"name": "Rocket League", "keywords": ["Rocket League"], "hashtags": ["#rocketleague"]}})
    assert gd.detect("Rocket League ranked", []) == "rl"


def test_quote_hook_uses_real_words():
    words = make_words([(0, "Bon on roule."), (2, "C'est pas possible !"), (4, "Bref.")])
    assert quote_hook(words) == "« C'EST PAS POSSIBLE ! »"


def test_hooks_never_invent_facts():
    assert is_grounded("IL NE S'ATTENDAIT PAS À ÇA 💀", "il roule tranquillement", [])
    assert not is_grounded("IL GAGNE 1000000 DOLLARS", "il roule tranquillement", [])
    assert not is_grounded("Rockstar l'a banni", "il roule tranquillement", [])
    assert is_grounded("Rockstar l'a banni", "rockstar a parlé", [])


def test_hook_generation_rules(settings):
    words = make_words([(0, "Non non non ! C'est pas possible !")])
    hooks = HookGenerationService(settings).generate("unexpected_reaction", words, "Non non non ! C'est pas possible !", "fr")
    assert 1 <= len(hooks) <= 3
    assert all({"text", "duration", "position", "style", "source"} <= set(h) for h in hooks)
    assert hooks[0]["source"] == "quote"
    en = HookGenerationService(settings).generate("fail", [], "", "en")
    assert en[0]["text"] == "THIS ENDED BADLY 💀"


def test_metadata_counts_and_relevance(settings):
    meta = MetadataGenerationService(settings).generate("fail", "je suis mort game over", "ÇA S'EST MAL TERMINÉ 💀", "gta6", "fr")
    assert len(meta["titles"]) == 3 and len(meta["captions"]) == 3
    assert 5 <= len(meta["hashtags"]) <= 10
    assert "#gta6" in meta["hashtags"] and "#fail" in meta["hashtags"]
    assert len(set(meta["hashtags"])) == len(meta["hashtags"])
    assert meta["titles"][1].startswith("GTA 6 :")
