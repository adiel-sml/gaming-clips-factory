"""Choix des moments par l'IA, profils de contenu, coupe des blancs, compilation."""
import subprocess

import numpy as np
from sqlmodel import Session

from app.models.entities import Highlight, Video
from app.services.ingestion import VideoIngestionService
from app.transcription.base import Word
from app.video import ffmpeg

from .conftest import FakeTranscriber, make_words, requires_ffmpeg


class ScriptedLLM:
    name = "scripted"
    available = True

    def __init__(self, moments=None, fail=False):
        self.moments = moments or []
        self.fail = fail
        self.prompts = []

    def generate_json(self, system, prompt, schema, max_tokens=4000):
        self.prompts.append((system, prompt))
        if self.fail:
            return None
        key = next(iter(schema["properties"]))
        if key == "moments" and "Fenêtre" in prompt:
            return {"moments": self.moments}
        return None   # hooks / métadonnées : repli sur les règles


@requires_ffmpeg
def test_llm_picks_moments_from_annotated_transcript(fresh_db, synthetic_video):
    from app.core.db import get_engine
    from app.services.pipeline import PipelineService

    llm = ScriptedLLM([{"start": "00:00:19", "end": "00:00:33", "score": 9, "category": "unexpected_reaction",
                        "hook": "IL SE FAIT PERCUTER", "reason": "Il se fait percuter en plein pont et explose."},
                       {"start": "00:00:05", "end": "00:00:20", "score": 5, "category": "quote",
                        "hook": "BOF", "reason": "rien"}])
    with Session(get_engine(), expire_on_commit=False) as db:
        video, _ = VideoIngestionService(db).import_file(synthetic_video, rights_confirmed=True)
        pipe = PipelineService(db, transcriber=FakeTranscriber(), llm=llm)
        hls = pipe.analyze(video.id)
        assert db.get(Video, video.id).stats["picker"] == "llm"
        assert len(hls) == 1, "le moment noté 5 doit être écarté (seuil 7)"
        h = hls[0]
        assert h.category == "unexpected_reaction" and h.reason.startswith("Il se fait percuter")
        assert h.start <= 19.2 and 32 <= h.end <= 34.5 and h.signals["llm_score"] == 9
        system, prompt = llm.prompts[0]
        assert "Stream ou vidéo gaming" in system                      # consigne du profil
        assert "[00:00:20] Attends attends" in prompt and "<<FORT +" in prompt   # transcription annotée
        clips = pipe.generate(h.id, ["main"])
        assert clips[0].hook["text"] == "IL SE FAIT PERCUTER" and clips[0].hook["source"] == "llm"


@requires_ffmpeg
def test_llm_failure_falls_back_to_rules(fresh_db, synthetic_video):
    from app.core.db import get_engine
    from app.services.pipeline import PipelineService

    with Session(get_engine(), expire_on_commit=False) as db:
        video, _ = VideoIngestionService(db).import_file(synthetic_video, rights_confirmed=True)
        hls = PipelineService(db, transcriber=FakeTranscriber(), llm=ScriptedLLM(fail=True)).analyze(video.id)
        assert hls and db.get(Video, video.id).stats["picker"] == "rules"


def test_sport_profile_vocabulary(settings):
    from app.models.entities import Video
    from app.scoring.highlight import HighlightDetectionService
    from app.scoring.lexicon import find_hits
    from app.services.pipeline import settings_for

    s = settings_for(Video(options={"profile": "sport"}))
    assert s.profile()["name"] == "sport" and s.get("video.max_clip_duration") == 35
    words = make_words([(0, "Quelle frappe ! Et c'est le but, penalty sifflé")])
    cats = {h.category for h in find_hits(words, HighlightDetectionService(s).lexicon)}
    assert {"action", "surprise"} <= cats
    assert "action" not in {h.category for h in find_hits(make_words([(0, "quelle frappe magnifique")]))}
    # un choix explicite à l'import prime sur le profil
    assert settings_for(Video(options={"profile": "sport", "max_clip_duration": 50})).get("video.max_clip_duration") == 50


def test_dead_air_only_cuts_quiet_pauses(settings, fresh_db):
    from app.core.db import get_engine
    from app.services.pipeline import PipelineService
    from app.video.analysis import AnalysisResult

    n = 80                                    # 40 s, pas de 0,5 s
    db = np.full(n, -20.0)
    db[16:22] = -45                           # 8-11 s : vrai blanc (calme)
    db[50:56] = -15                           # 25-28 s : pause de parole mais explosion sonore -> on garde
    analysis = AnalysisResult(0.5, 40, db.tolist(), [1.0] * n, [0] * n, [100.0] * n)
    words = [Word(0, 7.5, "a"), Word(11.5, 24.5, "b"), Word(28.5, 40, "c")]
    with Session(get_engine()) as session:
        pipe = PipelineService(session, transcriber=FakeTranscriber())
        cuts = pipe._dead_air(0, 40, words, analysis, settings)
        assert len(cuts) == 1 and 7.6 <= cuts[0][0] < cuts[0][1] <= 11.4
        f = pipe._time_mapper(cuts)
        removed = cuts[0][1] - cuts[0][0]
        assert f(5) == 5 and abs(f(20) - (20 - removed)) < 0.01


@requires_ffmpeg
def test_render_with_jump_cuts(tmp_path, settings):
    from app.clips.composition import VideoCompositionService
    from app.clips.tracking import NoFaceDetector
    from app.services.render import RenderService

    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=20",
                    "-f", "lavfi", "-i", "sine=f=300:d=20", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    plan = VideoCompositionService(settings, face_detector=NoFaceDetector()).plan(src, 1280, 720, 0, 20, layout="gameplay")
    out = tmp_path / "o.mp4"
    RenderService(settings).render(src, 0, 20, plan, out, None, True, 30, keep=[(0, 6), (9, 20)])
    info = ffmpeg.probe(out)
    assert abs(info.duration - 17) < 0.3 and (info.width, info.height) == (1080, 1920)


@requires_ffmpeg
def test_compilation(fresh_db, synthetic_video, pipeline_factory):
    from app.core.db import get_engine

    with Session(get_engine(), expire_on_commit=False) as db:
        video, _ = VideoIngestionService(db).import_file(synthetic_video, rights_confirmed=True)
        pipe = pipeline_factory(db)
        pipe.analyze(video.id)
        db.add(Highlight(video_id=video.id, rank=9, start=2, end=14, peak=6, duration=12, score=50,
                         category="quote", reason="test", confidence=0.6, transcript="salut tout le monde"))
        db.commit()
        clip = pipe.generate_compilation(video.id, count=3, piece_seconds=6)
        assert clip.variant == "compilation" and len(clip.crop["segments"]) == 2
        assert clip.status in ("ready", "needs_review"), clip.qc
        info = ffmpeg.probe(clip.file_path)
        expected = sum(b - a for a, b in clip.crop["segments"])
        assert abs(info.duration - expected) < 1.0 and (info.width, info.height) == (1080, 1920)
        assert clip.hook["text"] == "LES 2 MEILLEURS MOMENTS 🔥"
        first_seg = clip.crop["segments"][0]
        assert first_seg[0] <= 21.5 <= first_seg[1], "le meilleur moment passe en premier"
