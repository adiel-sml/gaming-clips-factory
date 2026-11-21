"""API REST, file d'attente, import, et test end-to-end du pipeline complet."""
import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.models.entities import Clip, Highlight, Job, Video
from app.services.ingestion import IngestionError, VideoIngestionService

from .conftest import FakeTranscriber, requires_ffmpeg

pytestmark = requires_ffmpeg


@pytest.fixture
def client(fresh_db):
    from fastapi.testclient import TestClient

    from app.api.main import app

    app.state.disable_worker = True
    app.state.pipeline_kwargs = {"transcriber": FakeTranscriber()}
    with TestClient(app) as c:
        yield c
    app.state.pipeline_kwargs = {}


# ------------------------------------------------------------------ import
def test_ingestion_requires_rights_and_deduplicates(fresh_db, synthetic_video):
    from app.core.db import get_engine

    with Session(get_engine()) as db:
        ing = VideoIngestionService(db)
        with pytest.raises(IngestionError, match="droit"):
            ing.import_file(synthetic_video)
        v1, existed1 = ing.import_file(synthetic_video, rights_confirmed=True)
        v2, existed2 = ing.import_file(synthetic_video, rights_confirmed=True)
        assert not existed1 and existed2 and v1.id == v2.id
        assert v1.duration > 39 and v1.width == 1280
        with pytest.raises(IngestionError, match="Domaine non autorisé"):
            ing.import_url("https://example.com/video.mp4", rights_confirmed=True)


# ------------------------------------------------------------------ end-to-end
def test_end_to_end_pipeline(fresh_db, synthetic_video, pipeline_factory, settings):
    """vidéo → transcription → highlight → clip → sous-titres → export."""
    from app.core.db import get_engine

    with Session(get_engine(), expire_on_commit=False) as db:
        video, _ = VideoIngestionService(db).import_file(synthetic_video, title="GTA 6 test", rights_confirmed=True)
        pipe = pipeline_factory(db)
        highlights = pipe.analyze(video.id)
        assert highlights, "le moment fort (20-23 s) doit être détecté"
        top = highlights[0]
        assert top.start <= 20.5 <= top.end, "le clip doit contenir l'événement"
        assert top.start <= 17.5, "contexte avant l'événement"
        assert settings.get('video.min_clip_duration') - 0.5 <= top.duration <= settings.get('video.max_clip_duration')
        assert db.get(Video, video.id).game == "gta6"

        clips = pipe.generate(top.id)
        assert [c.variant for c in clips] == ["main", "alt"]
        for c in clips:
            assert c.status == "ready", c.qc.get("reasons")
            assert Path(c.file_path).exists() and Path(c.thumbnail_path).exists()
            meta = json.loads(Path(c.metadata_path).read_text())
            for key in ("timing", "highlight", "transcript", "composition", "hook", "titles", "captions", "hashtags", "qc"):
                assert key in meta
            assert meta["highlight"]["score"] == top.score
            assert "#gta6" in meta["hashtags"]
            ass = Path(c.file_path).with_name("subtitles.ass").read_text()
            assert "Dialogue" in ass and "POSSIBLE" in ass.upper()
        assert clips[1].qc["plan"]["layout"] == "blur_fit"

        exported = pipe.export(clips[0].id)
        assert exported.exists() and exported.with_suffix(".json").exists() and exported.with_suffix(".srt").exists()
        assert db.get(Clip, clips[0].id).status == "exported"

        # ré-analyse : aucun doublon de moment
        pipe.analyze(video.id)
        n = len(db.exec(select(Highlight).where(Highlight.video_id == video.id, Highlight.status != "duplicate")).all())
        assert n == len(highlights)


# ------------------------------------------------------------------ API
def test_api_full_flow(client, synthetic_video):
    r = client.post("/videos", json={"source": str(synthetic_video), "rights_confirmed": False})
    assert r.status_code == 400

    r = client.post("/videos", json={"source": str(synthetic_video), "rights_confirmed": True, "title": "GTA 6 api",
                                     "options": {"clips_per_video": 3, "subtitle_style": "clean"}, "auto": "none"})
    assert r.status_code == 201, r.text
    vid = r.json()["video"]["id"]
    assert client.get("/videos").json()[0]["id"] == vid

    r = client.post(f"/videos/{vid}/analyze?sync=true", json={})
    assert r.status_code == 200, r.text
    hls = client.get(f"/videos/{vid}/highlights").json()
    assert hls and {"score", "category", "reason", "confidence", "transcript", "start_ts"} <= set(hls[0])

    r = client.post(f"/highlights/{hls[0]['id']}/generate?sync=true", json={"variants": ["main"]})
    assert r.status_code == 200, r.text
    clip = r.json()["clips"][0]
    assert clip["subtitle_style"] == "clean"  # option d'import appliquée
    assert clip["video_url"].startswith("/files/") and client.get(clip["video_url"]).status_code == 200

    dl = client.get(f"/clips/{clip['id']}/download")
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"] and dl.headers["content-type"] == "video/mp4"
    r = client.patch(f"/clips/{clip['id']}", json={"hook_text": "ATTENDS LA FIN", "start": hls[0]["start"] + 1, "title": "Mon titre"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    r = client.patch(f"/clips/{clip['id']}", json={"subtitle_style": "inexistant"})
    assert r.status_code == 400

    r = client.post(f"/clips/{clip['id']}/export", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert Path(body["path"]).exists() and body["clip"]["status"] == "exported"
    assert body["clip"]["hook"]["text"] == "ATTENDS LA FIN"

    stats = client.get("/stats").json()
    assert stats["videos"] == 1 and stats["clips_total"] >= 1 and stats["best_highlights"]
    assert client.get("/system").json()["subtitle_presets"]
    assert client.get("/docs").status_code == 200
    assert client.get("/clips/does-not-exist").status_code == 404


def test_api_queues_jobs(client, synthetic_video):
    r = client.post("/videos", json={"source": str(synthetic_video), "rights_confirmed": True, "auto": "analyze"})
    job = r.json()["job"]
    assert job["type"] == "analyze" and job["status"] == "queued"
    # même tâche demandée deux fois : pas de doublon dans la file
    vid = r.json()["video"]["id"]
    r2 = client.post(f"/videos/{vid}/analyze", json={})
    assert r2.json()["job"]["id"] == job["id"]


# ------------------------------------------------------------------ file d'attente
def test_queue_worker_processes_retries_and_resumes(fresh_db, synthetic_video, pipeline_factory):
    from datetime import timedelta

    from app.core.db import get_engine
    from app.core.utils import utcnow
    from app.workers.queue import JobQueue

    q = JobQueue()
    with Session(get_engine(), expire_on_commit=False) as db:
        video, _ = VideoIngestionService(db).import_file(synthetic_video, rights_confirmed=True)
        job = q.enqueue(db, "analyze", video.id)
        bad = q.enqueue(db, "analyze", "vid_inexistante")
        # tâche « running » abandonnée (crash) : doit être reprise
        stale = Job(type="analyze", target_id=video.id + "x", status="running", started_at=utcnow() - timedelta(hours=5))
        db.add(stale)
        db.commit()

    processed = q.run_worker(once=True, pipeline_factory=pipeline_factory)
    assert processed == 3
    with Session(get_engine()) as db:
        assert db.get(Job, job.id).status == "done"
        assert db.get(Job, bad.id).status == "failed" and "introuvable" in db.get(Job, bad.id).error
        assert db.get(Job, stale.id).status == "failed"  # reprise effectuée, puis échec (cible inexistante)
        assert db.get(Video, video.id).status == "analyzed"


# ------------------------------------------------------------------ transcription réelle
@pytest.mark.slow
def test_real_whisper_transcription(tmp_path):
    """Vrai modèle faster-whisper (tiny) sur la voix de la vidéo de test, si elle a été générée."""
    pytest.importorskip("faster_whisper")
    root = Path(__file__).resolve().parents[2]
    video = root / "data" / "test" / "test_stream_gta6.mp4"
    if not video.exists():
        pytest.skip("Lancer scripts/make_test_video.py d'abord")
    from app.transcription.service import FasterWhisperTranscriber
    from app.video import ffmpeg

    wav = tmp_path / "a.wav"
    ffmpeg.run(["ffmpeg", "-y", "-v", "error", "-t", "12", "-i", str(video), "-ac", "1", "-ar", "16000", str(wav)])
    try:
        tr = FasterWhisperTranscriber("tiny").transcribe(wav, "fr")
    except Exception as exc:  # modèle non téléchargeable (hors ligne)
        pytest.skip(f"Modèle indisponible : {exc}")
    text = " ".join(w.text for w in tr.words).lower()
    assert "salut" in text or "stream" in text
    assert all(w.end >= w.start for w in tr.words)
    assert tr.words[0].start < 3


def test_api_token_protection(client, monkeypatch):
    monkeypatch.setenv("GCF_API_TOKEN", "s3cret")
    assert client.get("/health").status_code == 200            # santé : publique
    assert client.get("/docs").status_code == 200              # documentation : publique
    assert client.get("/stats").status_code == 401
    assert client.get("/stats", headers={"Authorization": "Bearer faux"}).status_code == 401
    assert client.get("/stats", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/stats?token=s3cret").status_code == 200  # balises <video>/<img>
    assert client.get("/files/inexistant.mp4").status_code == 401
    pre = client.options("/stats", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert pre.status_code == 200                               # préflight CORS non bloqué


def test_orphaned_jobs_recovered_on_startup(fresh_db, synthetic_video):
    """Crash en pleine analyse : au redémarrage, la tâche repart ; après 3 interruptions, échec expliqué."""
    from app.core.db import get_engine
    from app.core.utils import utcnow
    from app.workers.queue import JobQueue

    with Session(get_engine(), expire_on_commit=False) as db:
        video, _ = VideoIngestionService(db).import_file(synthetic_video, rights_confirmed=True)
        video.status = "analyzing"
        fresh = Job(type="analyze", target_id=video.id, status="running", attempts=1, started_at=utcnow())
        worn = Job(type="analyze", target_id="vid_x", status="running", attempts=3, started_at=utcnow())
        db.add_all([video, fresh, worn])
        db.commit()
        q = JobQueue()
        assert q.requeue_stale(db) == 0                 # mode multi-workers : tâche récente laissée tranquille
        assert q.requeue_stale(db, all_running=True) == 2
        db.refresh(fresh), db.refresh(worn), db.refresh(video)
        assert fresh.status == "queued" and video.status == "queued"
        assert worn.status == "failed" and "mémoire" in worn.error


def test_analysis_reports_progress(fresh_db, synthetic_video, pipeline_factory):
    from app.core.db import get_engine

    seen = []
    with Session(get_engine(), expire_on_commit=False) as db:
        video, _ = VideoIngestionService(db).import_file(synthetic_video, rights_confirmed=True)
        pipe = pipeline_factory(db)
        original = db.commit

        def spy():
            p = (db.get(Video, video.id).stats or {}).get("progress")
            if p:
                seen.append(p["step"])
            original()

        db.commit = spy
        pipe.analyze(video.id)
        db.commit = original
        assert "Analyse de l'image et du son" in seen and "Détection des moments" in seen
        assert "progress" not in db.get(Video, video.id).stats   # retiré une fois terminé
