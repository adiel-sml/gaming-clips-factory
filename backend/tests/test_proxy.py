"""Liens : analyse sur une version légère (360p), HD téléchargée seulement pour les clips retenus."""
import subprocess

import pytest
from sqlmodel import Session

from app.services.ingestion import IngestionError, VideoIngestionService
from app.video import ffmpeg

from .conftest import requires_ffmpeg

pytestmark = requires_ffmpeg


@pytest.fixture
def remote(monkeypatch, synthetic_video, tmp_path):
    """Simule une VOD distante : version légère 640x360, passages HD découpés dans la vidéo 1280x720."""
    proxy = tmp_path / "proxy_src.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(synthetic_video), "-vf", "scale=640:360",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(proxy)], check=True)
    calls = []
    monkeypatch.setattr(VideoIngestionService, "_probe_url", lambda self, url: {"duration": 40, "title": "VOD"})

    def fake_download(self, url, out_dir, video, section=None, quality="full", name="source"):
        calls.append((quality, name, section))
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f"{name}.mp4"
        if quality == "proxy":
            subprocess.run(["cp", str(proxy), str(dst)], check=True)
        elif section is None:
            subprocess.run(["cp", str(synthetic_video), str(dst)], check=True)
        else:
            a, b = section
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(a), "-to", str(b), "-i", str(synthetic_video),
                            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(dst)], check=True)
        return dst

    monkeypatch.setattr(VideoIngestionService, "_download", fake_download)
    return calls


def test_proxy_analysis_and_hd_clips(fresh_db, remote, pipeline_factory):
    from app.core.db import get_engine
    from app.models.entities import Video

    with Session(get_engine(), expire_on_commit=False) as db:
        ing = VideoIngestionService(db)
        video, _ = ing.prepare_url("https://www.twitch.tv/videos/1", rights_confirmed=True)
        ing.download_url(video)
        assert remote[0][0] == "proxy" and remote[0][2] is None
        assert (video.width, video.height) == (640, 360) and video.options["remote_hd"]

        pipe = pipeline_factory(db)
        top = pipe.analyze(video.id)[0]
        assert top.start <= 20.5 <= top.end        # le moment fort est trouvé sur la version légère

        clips = pipe.generate(top.id, ["main"])
        clip = clips[0]
        assert clip.status == "ready", clip.qc.get("reasons")
        assert clip.qc["plan"]["hd_source"] == "segment"
        hd_calls = [c for c in remote if c[0] == "full"]
        assert len(hd_calls) == 1
        a, b = hd_calls[0][2]
        assert a <= clip.start and clip.end <= b and (b - a) <= (clip.end - clip.start) + 10.01  # ~1 min, pas 40 s
        info = ffmpeg.probe(clip.file_path)
        assert (info.width, info.height) == (1080, 1920)

        # re-rendu (ex. après une petite retouche) : le passage HD en cache est réutilisé
        clip.start = round(clip.start + 1, 2)
        db.add(clip)
        db.commit()
        pipe.render_clip(clip.id)
        assert len([c for c in remote if c[0] == "full"]) == 1
        assert db.get(Video, video.id).file_path.endswith("source.mp4")


def test_hd_unavailable_falls_back_to_proxy(fresh_db, remote, pipeline_factory, monkeypatch):
    from app.core.db import get_engine

    with Session(get_engine(), expire_on_commit=False) as db:
        ing = VideoIngestionService(db)
        video, _ = ing.prepare_url("https://www.twitch.tv/videos/2", rights_confirmed=True)
        ing.download_url(video)
        pipe = pipeline_factory(db)
        top = pipe.analyze(video.id)[0]

        def refuse(self, *a, **k):
            raise IngestionError("Twitch refuse")

        monkeypatch.setattr(VideoIngestionService, "fetch_hd_segment", refuse)
        clip = pipe.generate(top.id, ["main"])[0]
        assert clip.file_path and clip.qc["plan"]["hd_source"].startswith("fallback")
        assert any("version légère" in w for w in clip.qc["warnings"])
        assert (ffmpeg.probe(clip.file_path).width, ffmpeg.probe(clip.file_path).height) == (1080, 1920)


def test_full_mode_still_available(fresh_db, remote):
    from app.core.config import get_settings
    from app.core.db import get_engine

    s = get_settings()
    s["ingestion"] = {**s["ingestion"], "url_mode": "full"}
    with Session(get_engine(), expire_on_commit=False) as db:
        ing = VideoIngestionService(db, s)
        video, _ = ing.prepare_url("https://www.twitch.tv/videos/3", rights_confirmed=True)
        ing.download_url(video)
        assert remote[-1][0] == "full" and not video.options.get("remote_hd")
