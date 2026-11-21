"""Import d'un extrait (plage début/fin) : fichier local, URL de 10 h, API."""
import shutil

import pytest
from sqlmodel import Session

from app.core.utils import parse_time
from app.services.ingestion import IngestionError, VideoIngestionService
from app.video import ffmpeg

from .conftest import FakeTranscriber, requires_ffmpeg


@pytest.mark.parametrize("text,expected", [
    ("6:00:00", 21600), ("6h", 21600), ("6h30", 23400), ("6h30m15s", 23415), ("90m", 5400),
    ("90min", 5400), ("45s", 45), ("2m30", 150), ("21600", 21600), ("62:30", 3750), (" 7h ", 25200),
])
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("bad", ["demain", "30x", "6 30", "h"])
def test_parse_time_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_time(bad)


def test_parse_time_empty():
    assert parse_time("") is None and parse_time(None) is None


@requires_ffmpeg
def test_file_section_import_and_dedup(fresh_db, synthetic_video):
    from app.core.db import get_engine

    with Session(get_engine(), expire_on_commit=False) as db:
        ing = VideoIngestionService(db)
        v, existed = ing.import_file(synthetic_video, rights_confirmed=True, start="0:10", end="0:35")
        assert not existed
        real_start = v.options["section_start"]          # image-clé <= 10 s
        assert real_start <= 10 and v.options["section_requested_start"] == 10 and v.options["section_end"] == 35
        assert abs(v.duration - (35 - real_start)) < 0.3, (v.duration, real_start)
        assert "00:00:10" in v.title
        assert ffmpeg.probe(v.file_path).has_audio
        # même plage -> même vidéo ; autre plage ou vidéo entière -> nouvelle entrée
        assert ing.import_file(synthetic_video, rights_confirmed=True, start="10", end="35")[0].id == v.id
        other, existed2 = ing.import_file(synthetic_video, rights_confirmed=True, start="20s", end="40s")
        assert not existed2 and other.id != v.id
        full, _ = ing.import_file(synthetic_video, rights_confirmed=True)
        assert full.id not in (v.id, other.id) and abs(full.duration - 40) < 0.5
        with pytest.raises(IngestionError, match="après le début"):
            ing.import_file(synthetic_video, rights_confirmed=True, start="30", end="20")
        with pytest.raises(IngestionError, match="dépasse la durée"):
            ing.import_file(synthetic_video, rights_confirmed=True, start="1h")
        with pytest.raises(IngestionError, match="illisible"):
            ing.import_file(synthetic_video, rights_confirmed=True, start="demain")


@requires_ffmpeg
def test_url_section_only_downloads_the_range(fresh_db, synthetic_video, monkeypatch):
    """Vidéo YouTube de 10 h : sans plage -> refus clair ; avec 6h-7h -> seule la plage est demandée."""
    from app.core.db import get_engine

    calls = {}
    monkeypatch.setattr(VideoIngestionService, "_probe_url", lambda self, url: {"duration": 36000, "title": "Stream 10h"})

    def fake_download(self, url, out_dir, video, section=None, **kw):
        calls["section"] = section
        dst = out_dir / "source.mp4"
        shutil.copy(synthetic_video, dst)
        return dst

    monkeypatch.setattr(VideoIngestionService, "_download", fake_download)
    with Session(get_engine(), expire_on_commit=False) as db:
        ing = VideoIngestionService(db)
        with pytest.raises(IngestionError, match="trop longue"):
            ing.import_url("https://www.youtube.com/watch?v=abc", rights_confirmed=True)
        v, _ = ing.import_url("https://www.youtube.com/watch?v=abc", rights_confirmed=True, start="6:00:00", end="7h")
        assert calls["section"] == (21600, 25200)
        assert v.title.startswith("Stream 10h [06:00:00")
        assert v.options["section_start"] == 21600
        again, existed = ing.import_url("https://www.youtube.com/watch?v=abc", rights_confirmed=True, start="6h", end="7h")
        assert existed and again.id == v.id
        with pytest.raises(IngestionError, match="dépasse la durée"):
            ing.import_url("https://www.youtube.com/watch?v=abc", rights_confirmed=True, start="11h")


@requires_ffmpeg
def test_api_section_and_original_timestamps(fresh_db, synthetic_video):
    from fastapi.testclient import TestClient

    from app.api.main import app

    app.state.disable_worker = True
    app.state.pipeline_kwargs = {"transcriber": FakeTranscriber()}
    with TestClient(app) as client:
        r = client.post("/videos", json={"source": str(synthetic_video), "rights_confirmed": True,
                                         "start": "0:10", "end": "0:40", "auto": "none"})
        assert r.status_code == 201, r.text
        vid = r.json()["video"]["id"]
        assert client.post(f"/videos/{vid}/analyze?sync=true", json={}).status_code == 200
        hls = client.get(f"/videos/{vid}/highlights").json()
        assert hls
        h = hls[0]
        offset = h["source_offset"]
        assert 0 < offset <= 10                       # début réel = image-clé <= 10 s
        assert abs((h["start"] + offset) - parse_time(h["source_start_ts"][:8])) < 1.01
        bad = client.post("/videos", json={"source": str(synthetic_video), "rights_confirmed": True, "start": "abc"})
        assert bad.status_code == 400 and "illisible" in bad.json()["detail"]
    app.state.pipeline_kwargs = {}


def test_youtube_bot_check_gives_clear_message():
    from app.services.ingestion import BOT_CHECK_MSG, _download_error

    err = _download_error(Exception("ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies"), "x")
    assert str(err) == BOT_CHECK_MSG and "cookies" not in str(err)
    assert "privée" in str(_download_error(Exception("ERROR: Private video"), "x"))


@requires_ffmpeg
def test_api_url_import_is_async(fresh_db, synthetic_video, monkeypatch, pipeline_factory):
    """POST /videos avec une URL répond immédiatement ; le worker télécharge puis analyse."""
    import time

    from fastapi.testclient import TestClient

    from app.api.main import app
    from app.core.db import get_engine
    from app.models.entities import Job, Video
    from app.workers.queue import JobQueue

    monkeypatch.setattr(VideoIngestionService, "_probe_url", lambda self, url: {"duration": 36000, "title": "VOD"})

    def slow_download(self, url, out_dir, video, section=None, **kw):
        assert section == (16200, 19800)
        dst = out_dir / "source.mp4"
        shutil.copy(synthetic_video, dst)
        return dst

    monkeypatch.setattr(VideoIngestionService, "_download", slow_download)
    app.state.disable_worker = True
    with TestClient(app) as client:
        t0 = time.time()
        r = client.post("/videos", json={"source": "https://www.twitch.tv/videos/123", "rights_confirmed": True,
                                         "start": "4:30:0", "end": "5:30:0", "auto": "analyze"})
        assert r.status_code == 201, r.text
        assert time.time() - t0 < 5
        body = r.json()
        assert body["video"]["status"] == "downloading" and body["job"]["type"] == "import"
        vid = body["video"]["id"]
        # renvoyer la même demande pendant le téléchargement ne relance rien
        again = client.post("/videos", json={"source": "https://www.twitch.tv/videos/123", "rights_confirmed": True,
                                             "start": "4:30:0", "end": "5:30:0"})
        assert again.json()["already_imported"] and again.json()["video"]["id"] == vid

    assert JobQueue().run_worker(once=True, pipeline_factory=pipeline_factory) >= 1
    with Session(get_engine()) as db:
        v = db.get(Video, vid)
        assert v.status == "analyzed", v.error
        assert v.file_path and v.duration > 39
        job = db.get(Job, body["job"]["id"])
        assert job.status == "done" and job.result["highlights"]


def test_async_download_failure_marks_video_failed(fresh_db, monkeypatch):
    from app.core.db import get_engine
    from app.models.entities import Job, Video
    from app.workers.queue import JobQueue

    monkeypatch.setattr(VideoIngestionService, "_probe_url", lambda self, url: {"duration": 600, "title": "VOD"})

    def broken(self, *a, **k):
        raise IngestionError("YouTube refuse le téléchargement depuis ce serveur")

    monkeypatch.setattr(VideoIngestionService, "_download", broken)
    with Session(get_engine(), expire_on_commit=False) as db:
        v, _ = VideoIngestionService(db).prepare_url("https://www.twitch.tv/videos/9", rights_confirmed=True)
        job = JobQueue().enqueue(db, "import", v.id, {"then": "analyze"})
    JobQueue().run_worker(once=True)
    with Session(get_engine()) as db:
        assert db.get(Video, v.id).status == "failed" and "refuse" in db.get(Video, v.id).error
        assert db.get(Job, job.id).status == "failed"
