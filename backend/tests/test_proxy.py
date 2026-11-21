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
        assert remote[0][0] == "proxy" and remote[0][2] == (0.0, 40.0)   # toute la vidéo, mise en mémoire
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


def test_same_link_reuses_downloaded_ranges(fresh_db, synthetic_video, monkeypatch):
    """6:00-7:00 puis 6:30-7:30 : seule la demi-heure manquante est téléchargée ; puis 6:15-6:45 : rien."""
    from app.core.db import get_engine

    calls = []
    monkeypatch.setattr(VideoIngestionService, "_probe_url", lambda self, url: {"duration": 36000, "title": "VOD"})

    def fake(self, url, out_dir, video, section=None, quality="full", name="source"):
        calls.append((quality, section))
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f"{name}.mp4"
        dur = min(40.0, section[1] - section[0])   # on simule le passage avec la vidéo synthétique (40 s max)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", str(synthetic_video), "-t", str(dur),
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(dst)], check=True)
        return dst

    monkeypatch.setattr(VideoIngestionService, "_download", fake)
    url = "https://www.twitch.tv/videos/77"
    with Session(get_engine(), expire_on_commit=False) as db:
        ing = VideoIngestionService(db)
        v1, _ = ing.prepare_url(url, rights_confirmed=True, start="6:00:00", end="6:00:30")
        ing.download_url(v1)
        assert calls == [("proxy", (21600.0, 21630.0))]
        v2, _ = ing.prepare_url(url, rights_confirmed=True, start="6:00:15", end="6:00:45")
        ing.download_url(v2)
        assert calls[1] == ("proxy", (21630.0, 21645.0)), calls        # seulement le morceau manquant
        assert v2.stats["download"] == {"downloaded_seconds": 15.0, "reused_seconds": 15.0}
        assert abs(ffmpeg.probe(v2.file_path).duration - 30) < 0.5
        v3, _ = ing.prepare_url(url, rights_confirmed=True, start="6:00:05", end="6:00:40")
        ing.download_url(v3)
        assert len(calls) == 2 and v3.stats["download"]["downloaded_seconds"] == 0   # tout était déjà là
        assert ing.cached_ranges(url) == [(21600.0, 21645.0)]
        # passages HD partagés entre les plages du même lien
        p1, off1 = ing.fetch_hd_segment(v2, 18, 25)         # absolu 21633-21640
        p2, off2 = ing.fetch_hd_segment(v1, 33, 40)          # même passage vu depuis la première plage
        assert p1 == p2 and len([c for c in calls if c[0] == "full"]) == 1
        assert abs((off1 + 21615) - (off2 + 21600)) < 0.01
