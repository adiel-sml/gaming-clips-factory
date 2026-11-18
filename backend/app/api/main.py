"""API REST GamingClips Factory. Documentation interactive : /docs (Swagger) et /redoc."""
from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, func, select

from app.api.schemas import AnalyzeRequest, ClipPatch, ExportRequest, GenerateRequest, VideoCreate
from app.core.config import get_settings
from app.core.db import get_engine, get_session
from app.core.hardware import detect_hardware
from app.core.utils import fmt_ts, setup_logging, utcnow
from app.models.entities import Clip, ClipStatus, Highlight, Job, Video
from app.services.ingestion import IngestionError, VideoIngestionService
from app.services.pipeline import PipelineService
from app.workers.queue import EmbeddedWorker, JobQueue


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging("INFO", s.data_dir / "logs" / "api.log")
    get_engine()
    worker = None
    if s.get("queue.embedded_worker", True) and not getattr(app.state, "disable_worker", False):
        worker = EmbeddedWorker(JobQueue(s))
        worker.start()
    yield
    if worker:
        worker.shutdown()


app = FastAPI(
    title="GamingClips Factory API",
    version="0.1.0",
    description="Transforme des vidéos/streams gaming (dont vous avez les droits) en clips verticaux prêts à publier.",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().get("api.cors_origins", ["*"]),
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/files", StaticFiles(directory=str(get_settings().output_dir), check_dir=False), name="files")
app.state.pipeline_kwargs = {}   # injection (tests : transcripteur factice, etc.)


def pipeline(request: Request, db: Session) -> PipelineService:
    return PipelineService(db, **request.app.state.pipeline_kwargs)


def queue() -> JobQueue:
    return JobQueue(get_settings())


def file_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        rel = Path(path).resolve().relative_to(get_settings().output_dir.resolve())
    except ValueError:
        return None
    return f"/files/{rel.as_posix()}"


# ----------------------------------------------------------------- sérialisation
def video_out(v: Video, db: Session | None = None) -> dict:
    d = v.model_dump()
    if db is not None:
        d["highlight_count"] = db.exec(select(func.count()).select_from(Highlight).where(
            Highlight.video_id == v.id, Highlight.status != "duplicate")).one()
        d["clip_count"] = db.exec(select(func.count()).select_from(Clip).where(Clip.video_id == v.id)).one()
        job = db.exec(select(Job).where(Job.target_id == v.id).order_by(Job.created_at.desc())).first()
        d["last_job"] = {"id": job.id, "type": job.type, "status": job.status, "error": (job.error or "")[:300]} if job else None
    return d


def highlight_out(h: Highlight, db: Session | None = None) -> dict:
    d = h.model_dump()
    d.update(start_ts=fmt_ts(h.start), end_ts=fmt_ts(h.end))
    if db is not None:
        clips = db.exec(select(Clip).where(Clip.highlight_id == h.id)).all()
        d["clips"] = [clip_out(c) for c in clips]
        d["thumbnail_url"] = next((file_url(c.thumbnail_path) for c in clips if c.thumbnail_path), None)
    return d


def clip_out(c: Clip) -> dict:
    d = c.model_dump()
    d.update(video_url=file_url(c.file_path), thumbnail_url=file_url(c.thumbnail_path),
             metadata_url=file_url(c.metadata_path), duration=round(c.end - c.start, 2))
    return d


def get_or_404(db: Session, model, obj_id: str):
    obj = db.get(model, obj_id)
    if not obj:
        raise HTTPException(404, f"{model.__name__} introuvable : {obj_id}")
    return obj


# ----------------------------------------------------------------- système
@app.get("/health", tags=["système"])
def health():
    return {"status": "ok", "time": utcnow()}


@app.get("/system", tags=["système"])
def system():
    s = get_settings()
    return {"hardware": detect_hardware().to_dict(), "subtitle_presets": list(s.subtitle_presets().keys()),
            "layouts": ["auto", "gameplay", "streamer", "split", "webcam_overlay", "tracking", "blur_fit"],
            "games": {k: v.get("name", k) for k, v in s.games().items()},
            "defaults": {"min_clip_duration": s.get("video.min_clip_duration"),
                         "max_clip_duration": s.get("video.max_clip_duration"),
                         "clips_per_video": s.get("video.clips_per_video"),
                         "subtitle_style": s.get("subtitles.style"),
                         "aggressiveness": s.get("scoring.aggressiveness")}}


@app.get("/stats", tags=["système"])
def stats(db: Session = Depends(get_session)):
    """Chiffres du dashboard."""
    by_status = dict(db.exec(select(Clip.status, func.count()).group_by(Clip.status)).all())
    videos = db.exec(select(Video)).all()
    timings = [v.stats.get("analysis_timings", {}).get("total") for v in videos if v.stats]
    renders = db.exec(select(Clip.render_seconds).where(Clip.render_seconds > 0)).all()
    best = db.exec(select(Highlight).where(Highlight.status != "duplicate").order_by(Highlight.score.desc()).limit(5)).all()
    return {
        "videos": len(videos),
        "videos_failed": sum(v.status == "failed" for v in videos),
        "clips_total": sum(by_status.values()),
        "clips_by_status": by_status,
        "clips_pending": sum(by_status.get(k, 0) for k in (ClipStatus.PENDING, ClipStatus.RENDERING, ClipStatus.READY)),
        "clips_needs_review": by_status.get(ClipStatus.NEEDS_REVIEW, 0),
        "clips_validated": by_status.get(ClipStatus.APPROVED, 0) + by_status.get(ClipStatus.EXPORTED, 0),
        "clips_rejected": by_status.get(ClipStatus.REJECTED, 0),
        "avg_analysis_seconds": round(sum(t for t in timings if t) / max(1, len([t for t in timings if t])), 1),
        "avg_render_seconds": round(sum(renders) / max(1, len(renders)), 1),
        "jobs": dict(db.exec(select(Job.status, func.count()).group_by(Job.status)).all()),
        "errors": [{"video_id": v.id, "title": v.title, "error": (v.error or "")[:300]} for v in videos if v.status == "failed"]
                  + [{"job_id": j.id, "type": j.type, "error": (j.error or "").split("\n")[0][:300]}
                     for j in db.exec(select(Job).where(Job.status == "failed").limit(10)).all()],
        "best_highlights": [highlight_out(h) for h in best],
    }


# ----------------------------------------------------------------- vidéos
def _enqueue_auto(db: Session, video: Video, auto: str) -> Optional[dict]:
    if auto == "none":
        return None
    job = queue().enqueue(db, auto, video.id)
    return {"id": job.id, "type": job.type, "status": job.status}


@app.post("/videos", status_code=201, tags=["vidéos"])
def create_video(body: VideoCreate, db: Session = Depends(get_session)):
    """Importe une vidéo (URL autorisée ou fichier serveur), puis lance l'analyse en tâche de fond."""
    ing = VideoIngestionService(db)
    opts = body.options.model_dump(exclude_none=True)
    try:
        if body.source.startswith(("http://", "https://")):
            video, existed = ing.import_url(body.source, body.rights_confirmed, body.rights_note, body.title, opts)
        else:
            video, existed = ing.import_file(body.source, body.title, body.rights_confirmed, body.rights_note, opts)
    except IngestionError as exc:
        raise HTTPException(400, str(exc)) from exc
    if existed and opts:
        video.options = {**(video.options or {}), **opts}
        db.add(video)
        db.commit()
    job = _enqueue_auto(db, video, body.auto)
    return {"video": video_out(video, db), "already_imported": existed, "job": job}


@app.post("/videos/upload", status_code=201, tags=["vidéos"])
def upload_video(file: UploadFile = File(...), rights_confirmed: bool = Form(False), title: Optional[str] = Form(None),
                 auto: str = Form("analyze"), db: Session = Depends(get_session)):
    """Envoi d'un fichier vidéo depuis le navigateur."""
    uploads = get_settings().data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    safe = Path(file.filename or "video.mp4").name
    dst = uploads / safe
    with dst.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        video, existed = VideoIngestionService(db).import_file(dst, title or Path(safe).stem, rights_confirmed)
    except IngestionError as exc:
        dst.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    return {"video": video_out(video, db), "already_imported": existed, "job": _enqueue_auto(db, video, auto)}


@app.get("/videos", tags=["vidéos"])
def list_videos(db: Session = Depends(get_session)):
    return [video_out(v, db) for v in db.exec(select(Video).order_by(Video.created_at.desc())).all()]


@app.get("/videos/{video_id}", tags=["vidéos"])
def get_video(video_id: str, db: Session = Depends(get_session)):
    return video_out(get_or_404(db, Video, video_id), db)


@app.post("/videos/{video_id}/analyze", tags=["vidéos"])
def analyze_video(video_id: str, request: Request, body: AnalyzeRequest = AnalyzeRequest(),
                  sync: bool = Query(False, description="Exécuter immédiatement (bloquant)"),
                  db: Session = Depends(get_session)):
    get_or_404(db, Video, video_id)
    if sync:
        hls = pipeline(request, db).analyze(video_id, force=body.force)
        return {"highlights": [highlight_out(h) for h in hls]}
    job = queue().enqueue(db, "analyze", video_id, {"force": body.force})
    return {"job": job.model_dump()}


@app.post("/videos/{video_id}/pipeline", tags=["vidéos"])
def run_pipeline(video_id: str, request: Request, sync: bool = False, top_n: Optional[int] = None,
                 db: Session = Depends(get_session)):
    get_or_404(db, Video, video_id)
    if sync:
        return pipeline(request, db).run_full(video_id, top_n=top_n)
    return {"job": queue().enqueue(db, "pipeline", video_id, {"top_n": top_n}).model_dump()}


@app.get("/videos/{video_id}/highlights", tags=["moments"])
def list_highlights(video_id: str, include_duplicates: bool = False, db: Session = Depends(get_session)):
    get_or_404(db, Video, video_id)
    q = select(Highlight).where(Highlight.video_id == video_id)
    if not include_duplicates:
        q = q.where(Highlight.status != "duplicate")
    hls = db.exec(q.order_by(Highlight.score.desc())).all()
    return [highlight_out(h, db) for h in hls]


# ----------------------------------------------------------------- moments
@app.get("/highlights/{highlight_id}", tags=["moments"])
def get_highlight(highlight_id: str, db: Session = Depends(get_session)):
    return highlight_out(get_or_404(db, Highlight, highlight_id), db)


@app.post("/highlights/{highlight_id}/generate", tags=["moments"])
def generate_clip(highlight_id: str, request: Request, body: GenerateRequest = GenerateRequest(), sync: bool = False,
                  db: Session = Depends(get_session)):
    get_or_404(db, Highlight, highlight_id)
    if sync:
        clips = pipeline(request, db).generate(highlight_id, body.variants)
        return {"clips": [clip_out(c) for c in clips]}
    return {"job": queue().enqueue(db, "generate", highlight_id, {"variants": body.variants}).model_dump()}


@app.post("/highlights/{highlight_id}/reject", tags=["moments"])
def reject_highlight(highlight_id: str, db: Session = Depends(get_session)):
    h = get_or_404(db, Highlight, highlight_id)
    h.status = "rejected"
    db.add(h)
    db.commit()
    return highlight_out(h)


# ----------------------------------------------------------------- clips
@app.get("/clips", tags=["clips"])
def list_clips(status: Optional[str] = None, video_id: Optional[str] = None, db: Session = Depends(get_session)):
    q = select(Clip).order_by(Clip.created_at.desc())
    if status:
        q = q.where(Clip.status == status)
    if video_id:
        q = q.where(Clip.video_id == video_id)
    return [clip_out(c) for c in db.exec(q).all()]


@app.get("/clips/{clip_id}", tags=["clips"])
def get_clip(clip_id: str, db: Session = Depends(get_session)):
    c = get_or_404(db, Clip, clip_id)
    d = clip_out(c)
    h = db.get(Highlight, c.highlight_id)
    d["highlight"] = highlight_out(h) if h else None
    return d


@app.patch("/clips/{clip_id}", tags=["clips"])
def update_clip(clip_id: str, body: ClipPatch, db: Session = Depends(get_session)):
    """Édition : bornes, cadrage, style, hook, titre, statut. Un changement visuel repasse le clip en « pending »."""
    c: Clip = get_or_404(db, Clip, clip_id)
    video: Video = db.get(Video, c.video_id)
    visual = False
    if body.start is not None or body.end is not None:
        start = body.start if body.start is not None else c.start
        end = body.end if body.end is not None else c.end
        if not (0 <= start < end <= (video.duration or end)):
            raise HTTPException(400, "Bornes invalides")
        c.start, c.end, visual = round(start, 2), round(end, 2), True
    if body.layout is not None:
        c.layout, visual = body.layout or None, True
    if body.crop is not None:
        c.crop, visual = body.crop, True
    if body.subtitle_style is not None:
        if body.subtitle_style not in get_settings().subtitle_presets():
            raise HTTPException(400, f"Preset inconnu : {body.subtitle_style}")
        c.subtitle_style, visual = body.subtitle_style, True
    if body.hook_text is not None or body.hook_duration is not None:
        hook = dict(c.hook or {})
        if body.hook_text is not None:
            hook["text"], hook["source"] = body.hook_text, "manual"
        if body.hook_duration is not None:
            hook["duration"] = body.hook_duration
        c.hook, visual = hook, True
    if body.title is not None:
        c.title = body.title
    if body.hashtags is not None:
        c.hashtags = body.hashtags[:10]
    if body.performance is not None:
        c.performance = {**(c.performance or {}), **body.performance}
    if visual:
        c.status = ClipStatus.PENDING
    elif body.status is not None:
        c.status = body.status
    c.updated_at = utcnow()
    db.add(c)
    db.commit()
    return clip_out(c)


@app.post("/clips/{clip_id}/render", tags=["clips"])
def render_clip(clip_id: str, request: Request, sync: bool = False, db: Session = Depends(get_session)):
    get_or_404(db, Clip, clip_id)
    if sync:
        return clip_out(pipeline(request, db).render_clip(clip_id))
    return {"job": queue().enqueue(db, "render", clip_id).model_dump()}


@app.post("/clips/{clip_id}/export", tags=["clips"])
def export_clip(clip_id: str, request: Request, body: ExportRequest = ExportRequest(), db: Session = Depends(get_session)):
    get_or_404(db, Clip, clip_id)
    try:
        path = pipeline(request, db).export(clip_id, force=body.force)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"path": str(path), "clip": clip_out(db.get(Clip, clip_id))}


# ----------------------------------------------------------------- tâches
@app.get("/jobs", tags=["tâches"])
def list_jobs(status: Optional[str] = None, limit: int = 50, db: Session = Depends(get_session)):
    q = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if status:
        q = q.where(Job.status == status)
    return db.exec(q).all()


@app.get("/jobs/{job_id}", tags=["tâches"])
def get_job(job_id: str, db: Session = Depends(get_session)):
    return get_or_404(db, Job, job_id)
