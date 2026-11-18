"""Fixtures communes. Les tests utilisent des dossiers temporaires (jamais data/ ni output/)."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="gcf-tests-"))
os.environ["GCF_DATA_DIR"] = str(_TMP / "data")
os.environ["GCF_OUTPUT_DIR"] = str(_TMP / "output")
os.environ.pop("ANTHROPIC_API_KEY", None)   # pas d'appel LLM réel dans les tests
os.environ["GCF__SCORING__USE_LLM"] = "false"
os.environ["GCF__TRANSCRIPTION__BACKEND"] = "none"

from app.core.config import get_settings, reset_settings  # noqa: E402
from app.core.db import reset_engine  # noqa: E402
from app.transcription.base import Segment, Transcript, Word  # noqa: E402

FFMPEG = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="FFmpeg requis")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def _fresh_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Base SQLite isolée pour un test."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    reset_settings()
    reset_engine()
    from app.core.db import get_engine

    get_engine()
    yield
    reset_engine()


def make_words(spec: list[tuple[float, str]], word_dur: float = 0.28, gap: float = 0.06) -> list[Word]:
    """[(t_debut_phrase, "texte de la phrase"), ...] -> mots horodatés."""
    words = []
    for t0, sentence in spec:
        t = t0
        for tok in sentence.split():
            words.append(Word(round(t, 3), round(t + word_dur, 3), tok, 0.95))
            t += word_dur + gap
    return words


def make_transcript(spec: list[tuple[float, str]], language: str = "fr") -> Transcript:
    segs = []
    for t0, sentence in spec:
        ws = make_words([(t0, sentence)])
        segs.append(Segment(ws[0].start, ws[-1].end, sentence, ws))
    return Transcript(language=language, segments=segs, model="fake")


# Transcription scriptée de la vidéo synthétique (40 s, explosion sonore à 20-23 s)
SCRIPT = [
    (1.0, "Salut tout le monde, on lance une partie de GTA 6 à Vice City."),
    (5.0, "Je prends la voiture et je roule tranquillement en ville."),
    (10.0, "Le chat demande si le jeu est beau, franchement oui."),
    (15.0, "On arrive au pont, il n'y a personne."),
    (20.0, "Attends attends ! Non non non ! C'est pas possible ! Il m'a percuté !"),
    (25.5, "Bon, on respire, je prends une autre voiture."),
    (30.0, "On repart vers la banque, tranquille."),
    (35.0, "Merci pour les abonnements, à tout de suite."),
]


class FakeTranscriber:
    name = "fake"

    def __init__(self, spec=None):
        self.spec = spec or SCRIPT

    def transcribe(self, audio_path, language=None):
        return make_transcript(self.spec)


class FakeFaceDetector:
    """Renvoie toujours un petit visage en bas à gauche (webcam incrustée)."""

    def __init__(self, box=(0.06, 0.70, 0.08, 0.14)):
        from app.clips.tracking import Box

        self.box = Box(*box)

    def detect(self, frame):
        return [self.box]


@pytest.fixture(scope="session")
def synthetic_video() -> Path:
    """Vidéo 40 s 1280x720 30 fps : mire animée + audio calme avec explosion sonore à 20-23 s."""
    if not FFMPEG:
        pytest.skip("FFmpeg requis")
    out = _TMP / "assets" / "synthetic.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        audio = "aevalsrc='if(between(t,20,23),0.8,0.04)*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*3*t))':s=48000:d=40"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=40",
             "-f", "lavfi", "-i", audio, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(out)], check=True)
    return out


@pytest.fixture
def pipeline_factory():
    from app.services.pipeline import PipelineService

    def factory(session, **kw):
        return PipelineService(session, transcriber=FakeTranscriber(), **kw)

    return factory
