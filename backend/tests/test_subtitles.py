"""Génération des sous-titres ASS/SRT."""
import re

from app.subtitles.ass import PLAY_W, SAFE_MARGIN_X, SubtitleService, ass_color, ass_time

from .conftest import make_words

WORDS = make_words([(0.5, "Attends attends !"), (2.0, "Non non non c'est pas possible !"), (5.0, "On joue à GTA 6 ce soir")])


def test_ass_helpers():
    assert ass_color("#FF0000") == "&H000000FF"
    assert ass_time(3725.456) == "1:02:05.46"
    assert ass_time(1.999) == "0:00:02.00"


def test_events_are_synchronised_and_in_range(settings):
    res = SubtitleService(settings).build(WORDS, clip_duration=8.0, preset_name="gaming")
    assert res.events
    assert all(0 <= e.start < e.end <= 8.0 for e in res.events)
    assert all(a.start <= b.start for a, b in zip(res.events, res.events[1:]))
    # chaque mot apparaît au moment où il est prononcé
    first_word = WORDS[0]
    assert abs(res.events[0].start - first_word.start) < 0.01


def test_progressive_reveal_and_highlight(settings):
    res = SubtitleService(settings).build(WORDS, 8.0, preset_name="gaming")
    group_events = [e for e in res.events if e.start < 1.5]
    assert len(group_events[0].words) < len(group_events[-1].words), "les mots doivent apparaître progressivement"
    dialogue = [ln for ln in res.ass.splitlines() if ln.startswith("Dialogue")]
    assert any("\\c&H" in ln and "\\fscx" in ln for ln in dialogue)
    assert all("ATTENDS" in ln or "NON" in ln or "C'EST" in ln or "GTA" in ln or "POSSIBLE" in ln or "SOIR" in ln
               or "JOUE" in ln or "ON" in ln or "À" in ln or "CE" in ln or "6" in ln for ln in dialogue)


def test_keywords_emphasised(settings):
    svc = SubtitleService(settings)
    important, emotional = svc.keyword_flags(WORDS, extra_keywords=["GTA 6"])
    texts = [WORDS[i].text for i in important]
    assert any("possible" in t for t in texts)
    assert any(t in ("GTA", "6") for t in texts)
    assert emotional


def test_text_stays_in_safe_zone(settings):
    long_words = make_words([(0, "Anticonstitutionnellement extraordinairement incroyablement")])
    res = SubtitleService(settings).build(long_words, 5.0, preset_name="high_energy")
    assert all(e.width_est <= PLAY_W - 2 * SAFE_MARGIN_X + 1 for e in res.events)


def test_presets_come_from_yaml(settings):
    svc = SubtitleService(settings)
    assert {"clean", "gaming", "high_energy"} <= set(settings.subtitle_presets())
    clean = svc.build(WORDS, 8.0, preset_name="clean").ass
    gaming = svc.build(WORDS, 8.0, preset_name="gaming").ass
    assert "ATTENDS" in gaming and "Attends" in clean  # majuscules selon le preset


def test_hook_event_and_position(settings):
    res = SubtitleService(settings).build(WORDS, 8.0, subtitle_y=0.5,
                                          hook={"text": "IL NE S'ATTENDAIT PAS À ÇA 💀", "duration": 2.5, "y": 0.1})
    hook_line = next(ln for ln in res.ass.splitlines() if ",Hook," in ln)
    assert "0:00:00.00,0:00:02.50" in hook_line
    assert "💀" not in hook_line  # libass ne rend pas les emojis couleur : retirés de l'incrustation
    assert re.search(r"\\pos\(540,960\)", res.ass), "les sous-titres suivent la position demandée par le cadrage"


def test_srt_export(settings):
    srt = SubtitleService(settings).build(WORDS, 8.0).srt
    assert srt.startswith("1\n00:00:00,500 --> ")
    assert "possible" in srt
