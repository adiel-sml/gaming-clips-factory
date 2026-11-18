"""Scoring, sélection des moments, contexte, déduplication."""
import numpy as np

from app.clips.dedup import DeduplicationService, time_iou, transcript_similarity
from app.clips.selection import ClipSelectionService
from app.core.config import load_settings
from app.scoring.highlight import COMPONENTS, HighlightDetectionService, MomentCandidate
from app.video.analysis import AnalysisResult

from .conftest import make_transcript


def fake_analysis(duration=120.0, step=0.5, events=((60.0, 3.0),), motion_events=(), cuts=()) -> AnalysisResult:
    n = int(duration / step)
    rng = np.random.default_rng(0)
    db = -32 + rng.normal(0, 1.0, n)
    motion = 4 + rng.normal(0, 0.3, n)
    for t, d in events:
        i0 = int(t / step)
        db[max(0, i0 - 12):i0] = -45          # calme avant la tempête
        db[i0:i0 + int(d / step)] = -8        # explosion
    for t, d in motion_events:
        motion[int(t / step):int((t + d) / step)] = 25
    sc = [0] * n
    for t in cuts:
        sc[int(t / step)] = 1
    return AnalysisResult(step=step, duration=duration, audio_db=db.tolist(), motion=motion.tolist(), scene_cut=sc,
                          brightness=[100.0] * n, dhash=[], motion_center_x=[0.5] * n, probe={})


TRANSCRIPT = make_transcript([
    (40, "On roule tranquillement en ville."), (46, "Le chat demande si le jeu est beau."),
    (52, "On arrive vers le pont."), (55.5, "Il n'y a personne ici."),
    (60, "Attends ! Non non non ! C'est pas possible !"), (64, "Il m'a percuté de nulle part !"),
    (70, "Bon on respire."), (75, "On repart."),
])


def test_detects_the_real_moment(settings):
    analysis = fake_analysis(motion_events=((60, 4),), cuts=(60.5, 62))
    cands = HighlightDetectionService(settings).detect(analysis, TRANSCRIPT)
    assert cands, "aucun moment détecté"
    best = cands[0]
    assert 57 <= best.peak <= 66
    assert best.category in ("unexpected_reaction", "rage", "tension_release")
    assert 0 <= best.score <= 100 and best.score >= 60
    assert 0.5 <= best.confidence <= 0.97
    assert set(best.components) == set(COMPONENTS)
    assert all(0 <= v <= 1 for v in best.components.values())
    assert "pas possible" in best.reason or "pic sonore" in best.reason or "explosion" in best.reason


def test_quiet_video_yields_few_or_no_moments(settings):
    analysis = fake_analysis(events=())
    cands = HighlightDetectionService(settings).detect(analysis, make_transcript([(10, "on roule"), (50, "on roule encore")]))
    assert all(c.score < 60 for c in cands)


def test_weights_are_configurable():
    comps = {k: 0.0 for k in COMPONENTS}
    comps["humor"] = 1.0
    low = HighlightDetectionService(load_settings(overrides={"scoring": {"weights": {"humor": 0.1}}})).compute_score(comps)
    high = HighlightDetectionService(load_settings(overrides={"scoring": {"weights": {"humor": 10.0}}})).compute_score(comps)
    assert high > low


def test_aggressiveness_changes_threshold():
    analysis = fake_analysis(events=((30, 1.0), (90, 3.0)))
    tr = make_transcript([(30, "oh"), (90, "Non ! C'est pas possible !")])
    cons = HighlightDetectionService(load_settings(overrides={"scoring": {"aggressiveness": "conservative"}})).detect(analysis, tr)
    aggr = HighlightDetectionService(load_settings(overrides={"scoring": {"aggressiveness": "aggressive"}})).detect(analysis, tr)
    assert len(aggr) >= len(cons)


def _cand(peak, onset, end, score=80.0):
    return MomentCandidate(peak=peak, onset=onset, reaction_end=end, components={}, score=score,
                           category="unexpected_reaction", reason="", confidence=0.9)


def test_bounds_include_context_and_respect_durations(settings):
    analysis = fake_analysis()
    sel = ClipSelectionService(settings)
    c = _cand(61, 60, 66)
    start, end = sel.compute_bounds(c, TRANSCRIPT, analysis)
    assert 15 <= end - start <= 60
    assert 60 - start >= 3, "pas assez de contexte avant l'événement"
    assert 60 - start <= 12
    assert end >= 66 + 2, "la réaction doit avoir une conclusion"
    words = TRANSCRIPT.words
    assert not any(w.start < start < w.end for w in words), "début coupé au milieu d'un mot"
    assert not any(w.start < end < w.end for w in words), "fin coupée au milieu d'un mot"


def test_bounds_start_on_natural_sentence_start(settings):
    analysis = fake_analysis()
    start, _ = ClipSelectionService(settings).compute_bounds(_cand(61, 60, 66), TRANSCRIPT, analysis)
    sentence_starts = [s.start for s in TRANSCRIPT.segments]
    assert any(abs(start - s) < 0.15 for s in sentence_starts)


def test_bounds_clamped_at_video_edges(settings):
    analysis = fake_analysis(duration=30)
    start, end = ClipSelectionService(settings).compute_bounds(_cand(2, 1, 3), make_transcript([(1, "oh non !")]), analysis)
    assert start >= 0 and end <= 30 and end - start >= 15


def test_max_duration_enforced():
    s = load_settings(overrides={"video": {"min_clip_duration": 10, "max_clip_duration": 20}})
    analysis = fake_analysis()
    start, end = ClipSelectionService(s).compute_bounds(_cand(61, 50, 75), TRANSCRIPT, analysis)
    assert end - start <= 20.01


def test_time_iou_and_transcript_similarity():
    assert time_iou((0, 10), (5, 15)) == 5 / 15
    assert time_iou((0, 10), (20, 30)) == 0
    a = "non non non c'est pas possible il m'a percuté de nulle part"
    assert transcript_similarity(a, a + " incroyable") > 0.75
    assert transcript_similarity(a, "on roule tranquillement en ville vers la banque ce soir") < 0.3


def test_dedup_merges_overlaps_and_flags_replays(settings):
    d = DeduplicationService(settings)
    m1, m2, m3 = _cand(30, 29, 32, 90), _cand(33, 32, 35, 85), _cand(80, 79, 82, 70)
    m1.start, m1.end, m1.transcript = 20, 40, "non non non c'est pas possible il m'a percuté de nulle part"
    m2.start, m2.end, m2.transcript = 25, 45, "autre texte"
    m3.start, m3.end, m3.transcript = 70, 90, "non non non c'est pas possible il m'a percuté de nulle part encore"
    kept = d.filter_moments([m1, m2, m3])
    assert kept == [m1]
    assert m2.duplicate_of == 0 and m2.evidence["duplicate_reason"] == "overlap"
    assert m3.duplicate_of == 0 and m3.evidence["duplicate_reason"] == "transcript"
    assert (m1.start, m1.end) == (20, 45), "les moments qui se chevauchent sont fusionnés"


def test_dedup_visual_phash(settings):
    analysis = fake_analysis()
    h = format(0xF0F0F0F0F0F0F0F0, "016x")
    other = format(0x0F0F0F0F0F0F0F0F, "016x")
    analysis.dhash = [other] * 120
    for i in list(range(10, 30)) + list(range(80, 100)):
        analysis.dhash[i] = h
    a, b = _cand(20, 19, 22), _cand(90, 89, 92)
    a.start, a.end, b.start, b.end = 10, 30, 80, 100
    assert DeduplicationService(settings).is_duplicate(a, b, analysis) == "visual"
