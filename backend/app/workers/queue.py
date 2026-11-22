"""File de tâches persistée en base (MVP sans Redis).

- `enqueue` crée un Job « queued » ;
- un worker (`clips worker`, ou le thread embarqué de l'API) réclame les tâches
  atomiquement, les exécute et enregistre le résultat ;
- reprise après crash : les tâches « running » trop anciennes sont remises en file ;
- nouvelle tentative automatique jusqu'à `queue.max_attempts`.
Remplaçable par Redis + RQ/Celery : seule cette interface est utilisée par l'API et la CLI.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import timedelta

from sqlalchemy import update
from sqlmodel import Session, select

from app.core.config import Settings, get_settings
from app.core.db import get_engine
from app.core.utils import get_logger, utcnow
from app.models.entities import Job, Video, VideoStatus

log = get_logger("queue")


class JobQueue:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def enqueue(self, session: Session, job_type: str, target_id: str, payload: dict | None = None,
                dedupe: bool = True) -> Job:
        if dedupe:
            existing = session.exec(select(Job).where(Job.type == job_type, Job.target_id == target_id,
                                                      Job.status.in_(["queued", "running"]))).first()
            if existing:
                return existing
        job = Job(type=job_type, target_id=target_id, payload=payload or {})
        session.add(job)
        if job_type in ("analyze", "pipeline"):
            video = session.get(Video, target_id)
            if video and video.status not in (VideoStatus.ANALYZING, VideoStatus.DOWNLOADING):
                video.status = VideoStatus.QUEUED
                session.add(video)
        session.commit()
        session.refresh(job)
        return job

    def requeue_stale(self, session: Session, all_running: bool = False) -> int:
        """Remet en file les tâches interrompues (crash, manque de mémoire, redéploiement).

        `all_running=True` (worker unique, ex. worker embarqué au démarrage de l'API) : toute tâche
        « running » est forcément orpheline. Sinon, seulement celles plus vieilles que `stale_after`.
        Une tâche interrompue trop souvent passe en échec avec une explication."""
        q = select(Job).where(Job.status == "running")
        if not all_running:
            q = q.where(Job.started_at < utcnow() - timedelta(seconds=int(self.s.get("queue.stale_after", 7200))))
        max_attempts = int(self.s.get("queue.max_attempts", 3))
        jobs = session.exec(q).all()
        for j in jobs:
            video = session.get(Video, j.target_id) if j.type in ("analyze", "pipeline", "import") else None
            if j.attempts >= max_attempts:
                j.status, j.finished_at = "failed", utcnow()
                j.error = (f"Traitement interrompu {j.attempts} fois (le serveur a redémarré en cours de route, "
                           "souvent par manque de mémoire). Essayez un extrait plus court ou un serveur avec plus de RAM.")
                if video:
                    video.status, video.error = VideoStatus.FAILED, j.error
            else:
                j.status = "queued"
                if video:
                    video.status = VideoStatus.DOWNLOADING if j.type == "import" else VideoStatus.QUEUED
            session.add(j)
            if video:
                session.add(video)
        session.commit()
        return len(jobs)

    def claim(self, session: Session) -> Job | None:
        candidates = session.exec(select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(5)).all()
        for job in candidates:
            res = session.exec(  # type: ignore[call-overload]
                update(Job).where(Job.id == job.id, Job.status == "queued")
                .values(status="running", started_at=utcnow(), attempts=Job.attempts + 1)
            )
            session.commit()
            if res.rowcount == 1:
                session.refresh(job)
                return job
        return None

    def execute(self, session: Session, job: Job, pipeline_factory=None) -> Job:
        from app.services.pipeline import PipelineService

        pipe = (pipeline_factory or (lambda s: PipelineService(s)))(session)
        try:
            p = job.payload or {}
            if job.type == "import":
                from app.services.ingestion import VideoIngestionService

                video = session.get(Video, job.target_id)
                if video is None:
                    raise LookupError(f"Vidéo introuvable : {job.target_id}")
                VideoIngestionService(session).download_url(video)
                result: dict = {"downloaded": True}
                then = p.get("then", "none")
                if then == "analyze":
                    result["highlights"] = [h.id for h in pipe.analyze(video.id)]
                elif then == "pipeline":
                    result.update(pipe.run_full(video.id))
            elif job.type == "analyze":
                hls = pipe.analyze(job.target_id, force=p.get("force", False))
                result = {"highlights": [h.id for h in hls]}
            elif job.type == "generate":
                clips = pipe.generate(job.target_id, p.get("variants"))
                result = {"clips": [c.id for c in clips]}
            elif job.type == "compilation":
                clip = pipe.generate_compilation(job.target_id, int(p.get("count", 5)), float(p.get("piece_seconds", 10)))
                result = {"clip": clip.id}
            elif job.type == "render":
                result = {"clip": pipe.render_clip(job.target_id).id}
            elif job.type == "pipeline":
                result = pipe.run_full(job.target_id, top_n=p.get("top_n"), force=p.get("force", False))
            elif job.type == "export":
                result = {"path": str(pipe.export(job.target_id, force=p.get("force", False)))}
            else:
                raise ValueError(f"Type de tâche inconnu : {job.type}")
            job.status, job.result, job.error = "done", result, None
        except Exception as exc:
            log.error("Tâche %s échouée : %s", job.id, exc)
            session.rollback()
            job = session.get(Job, job.id) or job
            retry = job.attempts < int(self.s.get("queue.max_attempts", 3)) and not isinstance(exc, (LookupError, ValueError))
            job.status = "queued" if retry else "failed"
            job.error = f"{exc}\n{traceback.format_exc()[-1500:]}"
        job.finished_at = utcnow()
        session.add(job)
        session.commit()
        return job

    def run_worker(self, stop: threading.Event | None = None, once: bool = False, pipeline_factory=None,
                   requeue_all: bool = False) -> int:
        stop = stop or threading.Event()
        processed = 0
        with Session(get_engine(), expire_on_commit=False) as session:
            n = self.requeue_stale(session, all_running=requeue_all)
            if n:
                log.info("%d tâche(s) interrompue(s) remise(s) en file", n)
        interval = float(self.s.get("queue.poll_interval", 2))
        from app.services.retention import PurgeScheduler

        purger = PurgeScheduler(self.s)
        while not stop.is_set():
            with Session(get_engine(), expire_on_commit=False) as session:
                if not once:
                    purger.maybe_run(session)       # purge 48 h, au plus une fois par heure
                job = self.claim(session)
                if job:
                    log.info("→ %s %s (%s)", job.type, job.target_id, job.id)
                    self.execute(session, job, pipeline_factory)
                    processed += 1
                    continue
            if once:
                break
            stop.wait(interval)
        return processed


class EmbeddedWorker:
    """Thread de traitement lancé avec l'API (mode MVP « tout-en-un »)."""

    def __init__(self, queue: JobQueue):
        self.queue = queue
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="gcf-worker", daemon=True)

    def _run(self) -> None:
        first = True
        while not self.stop.is_set():
            try:
                # au démarrage de l'API, toute tâche « running » vient d'un processus mort : reprise immédiate
                self.queue.run_worker(self.stop, requeue_all=first)
                first = False
            except Exception:  # pragma: no cover - ne jamais tuer le thread
                log.exception("Worker embarqué : erreur, redémarrage dans 5 s")
                time.sleep(5)

    def start(self) -> None:
        self.thread.start()

    def shutdown(self) -> None:
        self.stop.set()
