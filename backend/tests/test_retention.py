"""Purge automatique 48 h : vidéos, extraits et clips inactifs supprimés ; rien d'actif n'est touché."""
import os
import time
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.core.config import get_settings, reset_settings
from app.core.db import get_engine
from app.core.utils import utcnow
from app.models.entities import Clip, Highlight, Job, Video, VideoStatus
from app.services.retention import PurgeScheduler, expires_at, purge


@pytest.fixture
def env(tmp_path, monkeypatch, fresh_db):
    monkeypatch.setenv("GCF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GCF_OUTPUT_DIR", str(tmp_path / "output"))
    reset_settings()
    return get_settings()


def _video(db, s, age_h, status=VideoStatus.ANALYZED, url=None):
    old = utcnow() - timedelta(hours=age_h)
    v = Video(title="t", status=status, source_url=url, created_at=old, updated_at=old)
    db.add(v)
    db.commit()
    vdir = s.data_dir / "videos" / v.id
    vdir.mkdir(parents=True)
    (vdir / "source.mp4").write_bytes(b"x" * 1000)
    v.file_path = str(vdir / "source.mp4")
    h = Highlight(video_id=v.id, start=0, end=10, peak=5, duration=10, score=50, category="action", reason="",
                  confidence=0.5)
    db.add_all([v, h])
    db.commit()
    c = Clip(video_id=v.id, highlight_id=h.id, start=0, end=10, created_at=old, updated_at=old)
    db.add(c)
    db.commit()
    cdir = s.output_dir / v.id / c.id
    cdir.mkdir(parents=True)
    (cdir / "clip.mp4").write_bytes(b"y" * 2000)
    return v, c


def test_purge_removes_inactive_videos_and_keeps_recent(env):
    s = env
    with Session(get_engine(), expire_on_commit=False) as db:
        old, _ = _video(db, s, 50)
        recent, _ = _video(db, s, 2)
        old_but_clip_edited, clip = _video(db, s, 60)
        clip.updated_at = utcnow() - timedelta(hours=1)   # modifié il y a 1 h : on garde tout
        db.add(clip)
        db.commit()
        busy, _ = _video(db, s, 80, status=VideoStatus.ANALYZING)

        dry = purge(db, s, dry_run=True)
        assert dry.videos == [old.id] and (s.data_dir / "videos" / old.id).exists()

        rep = purge(db, s)
        assert rep.videos == [old.id] and rep.freed_bytes >= 3000
        assert not (s.data_dir / "videos" / old.id).exists() and not (s.output_dir / old.id).exists()
        assert db.get(Video, old.id) is None
        assert not db.exec(select(Clip).where(Clip.video_id == old.id)).all()
        assert not db.exec(select(Highlight).where(Highlight.video_id == old.id)).all()
        for v in (recent, old_but_clip_edited, busy):
            assert db.get(Video, v.id) is not None and (s.data_dir / "videos" / v.id).exists()


def test_purge_skips_video_with_pending_job(env):
    s = env
    with Session(get_engine(), expire_on_commit=False) as db:
        v, c = _video(db, s, 100)
        db.add(Job(type="render", target_id=c.id, status="queued", created_at=utcnow() - timedelta(hours=99)))
        db.commit()
        assert purge(db, s).videos == []


def test_purge_cleans_old_cached_extracts_and_uploads(env):
    s = env
    src = s.data_dir / "sources" / "abc123" / "hd"
    src.mkdir(parents=True)
    old_f, new_f = src / "hd_0_10.mp4", src / "hd_10_20.mp4"
    old_f.write_bytes(b"a" * 500)
    new_f.write_bytes(b"b" * 500)
    up = s.data_dir / "uploads"
    up.mkdir(parents=True)
    old_up = up / "vieux.mp4"
    old_up.write_bytes(b"c" * 500)
    t = time.time() - 49 * 3600
    os.utime(old_f, (t, t))
    os.utime(old_up, (t, t))
    with Session(get_engine(), expire_on_commit=False) as db:
        rep = purge(db, s)
    assert not old_f.exists() and not old_up.exists() and new_f.exists()
    assert rep.files == 2


def test_expiry_and_disable(env, monkeypatch):
    s = env
    with Session(get_engine(), expire_on_commit=False) as db:
        v, _ = _video(db, s, 10)
        exp = expires_at(db, v, s)
        assert timedelta(hours=37) < exp - utcnow() < timedelta(hours=39)
        old, _ = _video(db, s, 100)
        monkeypatch.setitem(s, "retention", {"enabled": False, "hours": 48})
        assert expires_at(db, v, s) is None and purge(db, s).videos == []
        monkeypatch.setitem(s, "retention", {"enabled": True, "hours": 48, "check_every_minutes": 60})
        sched = PurgeScheduler(s)
        assert sched.maybe_run(db).videos == [old.id]
        assert sched.maybe_run(db) is None, "pas plus d'une purge par heure"
