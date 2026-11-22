"""Purge automatique : libère l'espace disque du serveur.

Une vidéo (lien ou fichier envoyé) et tout ce qui en découle — extraits téléchargés, analyse, clips
rendus — est supprimée quand personne n'y a touché depuis `retention.hours` (48 h par défaut).
« Toucher » = import, analyse, génération, rendu, ou modification d'un clip.
Les fichiers en dehors des dossiers de l'application (ex : vidéo locale importée en CLI) ne sont jamais
supprimés : seule leur fiche disparaît."""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from app.core.config import Settings, get_settings
from app.core.utils import get_logger, utcnow
from app.models.entities import Clip, Highlight, Job, Video, VideoStatus

log = get_logger("retention")

BUSY_VIDEO = {VideoStatus.DOWNLOADING, VideoStatus.QUEUED, VideoStatus.ANALYZING}


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)   # SQLite rend des dates « naïves » (UTC)


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


@dataclass
class PurgeReport:
    videos: list[str] = field(default_factory=list)
    files: int = 0
    freed_bytes: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {"videos": self.videos, "files": self.files, "freed_mb": round(self.freed_bytes / 1e6, 1),
                "dry_run": self.dry_run}


def last_activity(db: Session, video: Video) -> datetime:
    dates = [_aware(video.updated_at), _aware(video.created_at)]
    dates += [_aware(c.updated_at) for c in db.exec(select(Clip).where(Clip.video_id == video.id)).all()]
    dates += [_aware(j.finished_at or j.started_at or j.created_at)
              for j in db.exec(select(Job).where(Job.target_id == video.id)).all()]
    return max(d for d in dates if d is not None)


def expires_at(db: Session, video: Video, s: Settings | None = None) -> datetime | None:
    s = s or get_settings()
    hours = float(s.get("retention.hours", 48) or 0)
    if not s.get("retention.enabled", True) or hours <= 0:
        return None
    return last_activity(db, video) + timedelta(hours=hours)


def _video_busy(db: Session, video: Video) -> bool:
    if video.status in BUSY_VIDEO:
        return True
    clip_ids = [c.id for c in db.exec(select(Clip).where(Clip.video_id == video.id)).all()]
    targets = [video.id, *clip_ids, *[h.id for h in db.exec(select(Highlight).where(Highlight.video_id == video.id)).all()]]
    active = db.exec(select(Job).where(Job.target_id.in_(targets), Job.status.in_(["queued", "running"]))).first()
    return active is not None


def purge(db: Session, s: Settings | None = None, now: datetime | None = None, dry_run: bool = False) -> PurgeReport:
    s = s or get_settings()
    report = PurgeReport(dry_run=dry_run)
    hours = float(s.get("retention.hours", 48) or 0)
    if not s.get("retention.enabled", True) or hours <= 0:
        return report
    now = now or utcnow()
    limit = now - timedelta(hours=hours)
    data, out = s.data_dir, s.output_dir

    def remove(p: Path) -> None:
        if not p.exists():
            return
        report.freed_bytes += _size(p)
        report.files += sum(1 for f in p.rglob("*") if f.is_file()) if p.is_dir() else 1
        if not dry_run:
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)

    # 1) vidéos inactives depuis plus de 48 h : fichiers + fiches
    for video in db.exec(select(Video)).all():
        if _video_busy(db, video) or last_activity(db, video) > limit:
            continue
        report.videos.append(video.id)
        remove(data / "videos" / video.id)
        remove(out / video.id)
        if video.file_path and _inside(Path(video.file_path), data):   # jamais en dehors de data/
            remove(Path(video.file_path))
        if dry_run:
            continue
        clips = db.exec(select(Clip).where(Clip.video_id == video.id)).all()
        hls = db.exec(select(Highlight).where(Highlight.video_id == video.id)).all()
        targets = [video.id, *[c.id for c in clips], *[h.id for h in hls]]
        for job in db.exec(select(Job).where(Job.target_id.in_(targets))).all():
            db.delete(job)
        for row in [*clips, *hls]:
            db.delete(row)
        db.commit()
        db.delete(video)
        db.commit()

    # 2) fichiers isolés trop vieux : extraits en cache par lien, envois, dossier « ready »
    kept = {str(Path(v.file_path).resolve()) for v in db.exec(select(Video)).all() if v.file_path}
    busy_sources = {v.source_url for v in db.exec(select(Video)).all() if v.source_url and _video_busy(db, v)}
    cutoff = limit.timestamp()
    import hashlib

    busy_dirs = {hashlib.sha1(u.encode()).hexdigest()[:16] for u in busy_sources}
    for root in (data / "sources", data / "uploads", out / "ready"):
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or str(f.resolve()) in kept or f.stat().st_mtime > cutoff:
                continue
            if root.name == "sources" and f.relative_to(root).parts[0] in busy_dirs:
                continue
            remove(f)
        if not dry_run:   # dossiers vidés
            for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
    if report.videos or report.files:
        log.info("Purge%s : %d vidéo(s), %d fichier(s), %.1f Mo libérés", " (simulation)" if dry_run else "",
                 len(report.videos), report.files, report.freed_bytes / 1e6)
    return report


class PurgeScheduler:
    """Lance la purge au plus une fois par `retention.check_every_minutes` (appelé depuis la boucle du worker)."""

    def __init__(self, s: Settings | None = None):
        self.s = s or get_settings()
        self.last = 0.0

    def maybe_run(self, db: Session) -> PurgeReport | None:
        every = float(self.s.get("retention.check_every_minutes", 60)) * 60
        if time.monotonic() - self.last < every and self.last:
            return None
        self.last = time.monotonic()
        try:
            return purge(db, self.s)
        except Exception:  # pragma: no cover - la purge ne doit jamais bloquer le traitement
            log.exception("Purge automatique : erreur")
            db.rollback()
            return None
