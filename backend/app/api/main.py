"""API REST GamingClips Factory. Documentation interactive : /docs (Swagger) et /redoc."""
from __future__ import annotations

import hmac
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, func, select

from app.api.schemas import AnalyzeRequest, ProbeRequest, ClipPatch, ExportRequest, GenerateRequest, VideoCreate
from app.core.config import get_settings
from app.core.db import get_engine, get_session
from app.core.hardware import detect_hardware
from app.core.utils import fmt_ts, setup_logging, utcnow
from app.models.entities import Clip, ClipStatus, Highlight, Job, Video
from app.services.ingestion import IngestionError, VideoIngestionService
from app.services.pipeline import PipelineService
from app.services import retention
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
PUBLIC_PATHS = {"/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


def _cors_origins() -> list[str]:
    env = os.environ.get("GCF_CORS_ORIGINS")
    if env:
        return [o.strip() for o in env.split(",") if o.strip()]
    return get_settings().get("api.cors_origins", ["*"])


@app.middleware("http")
async def require_token(request: Request, call_next):
    """Si GCF_API_TOKEN est défini, toute requête doit fournir ce jeton :
    en-tête `Authorization: Bearer <jeton>` ou paramètre `?token=` (balises <video>/<img>)."""
    expected = os.environ.get("GCF_API_TOKEN")
    if expected and request.method != "OPTIONS" and request.url.path not in PUBLIC_PATHS:
        header = request.headers.get("authorization", "")
        given = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return JSONResponse({"detail": "Jeton d'accès manquant ou invalide"}, status_code=401)
    return await call_next(request)


app.add_middleware(CORSMiddleware, allow_origins=_cors_origins(),
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
        d["expires_at"] = retention.expires_at(db, v)
    return d


def _expiry_lookup(db: Session):
    """Date de suppression automatique par vidéo (calculée une fois par vidéo et par requête)."""
    cache: dict[str, object] = {}

    def get(video_id: str):
        if video_id not in cache:
            v = db.get(Video, video_id)
            cache[video_id] = retention.expires_at(db, v) if v else None
        return cache[video_id]
    return get


def highlight_out(h: Highlight, db: Session | None = None, offset: float | None = None) -> dict:
    d = h.model_dump()
    if offset is None and db is not None:
        v = db.get(Video, h.video_id)
        offset = float((v.options or {}).get("section_start", 0)) if v else 0.0
    offset = offset or 0.0
    # start/end : position dans l'extrait importé ; source_* : position dans la vidéo d'origine
    d.update(start_ts=fmt_ts(h.start), end_ts=fmt_ts(h.end), source_offset=offset,
             source_start_ts=fmt_ts(offset + h.start), source_end_ts=fmt_ts(offset + h.end))
    if db is not None:
        clips = db.exec(select(Clip).where(Clip.highlight_id == h.id)).all()
        d["clips"] = [clip_out(c) for c in clips]
        d["thumbnail_url"] = next((file_url(c.thumbnail_path) for c in clips if c.thumbnail_path), None)
    return d


def clip_out(c: Clip) -> dict:
    d = c.model_dump()
    segments = (c.crop or {}).get("segments")
    duration = sum(b - a for a, b in segments) if segments else c.end - c.start
    metrics = (c.qc or {}).get("metrics") or {}
    d.update(video_url=file_url(c.file_path), thumbnail_url=file_url(c.thumbnail_path),
             metadata_url=file_url(c.metadata_path), duration=round(metrics.get("duration") or duration, 2))
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


def storage_info() -> dict:
    """Le stockage survit-il aux redéploiements ? (Railway : volume monté via RAILWAY_VOLUME_MOUNT_PATH)."""
    s = get_settings()
    data = s.data_dir.resolve()
    out = s.output_dir.resolve()
    on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    persistent = None
    if on_railway:
        persistent = bool(mount) and all(str(p).startswith(str(Path(mount).resolve())) for p in (data, out))
    usage = shutil.disk_usage(data)
    return {"data_dir": str(data), "output_dir": str(out), "platform": "railway" if on_railway else "autre",
            "volume_mount": mount, "persistent": persistent,
            "free_gb": round(usage.free / 1e9, 1), "total_gb": round(usage.total / 1e9, 1)}


@app.get("/system", tags=["système"])
def system():
    s = get_settings()
    return {"hardware": detect_hardware().to_dict(), "storage": storage_info(), "subtitle_presets": list(s.subtitle_presets().keys()),
            "layouts": ["auto", "gameplay", "streamer", "split", "webcam_overlay", "tracking", "blur_fit"],
            "games": {k: v.get("name", k) for k, v in s.games().items()},
            "profiles": {k: v.get("label", k) for k, v in s.profiles().items()},
            "llm_picker": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
            "retention": {"enabled": bool(s.get("retention.enabled", True)), "hours": s.get("retention.hours", 48)},
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
        "best_highlights": [highlight_out(h, db) for h in best],
    }


# ----------------------------------------------------------------- vidéos
def _enqueue_auto(db: Session, video: Video, auto: str) -> Optional[dict]:
    if auto == "none":
        return None
    job = queue().enqueue(db, auto, video.id)
    return {"id": job.id, "type": job.type, "status": job.status}


@app.post("/probe", tags=["vidéos"])
def probe_source(body: ProbeRequest, db: Session = Depends(get_session)):
    """Infos d'un lien avant import (titre, durée, miniature) : rien n'est téléchargé."""
    from urllib.parse import urlparse

    url = body.source.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Collez un lien commençant par https://")
    host = (urlparse(url).hostname or "").lower()
    allowed = get_settings().get("ingestion.allowed_url_domains", []) or []
    if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
        raise HTTPException(400, f"Site non pris en charge : {host}. Sites acceptés : {', '.join(allowed)}")
    try:
        info = VideoIngestionService(db)._probe_url(url)
    except IngestionError as exc:
        raise HTTPException(400, str(exc)) from exc
    platform = next((p for p in ("youtube", "twitch", "vimeo", "kick") if p in host), host)
    ing = VideoIngestionService(db)
    return {"title": info.get("title"), "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader") or info.get("channel"), "platform": platform,
            "is_live": bool(info.get("is_live")), "cached_ranges": ing.cached_ranges(url)}


@app.post("/videos", status_code=201, tags=["vidéos"])
def create_video(body: VideoCreate, db: Session = Depends(get_session)):
    """Importe une vidéo (URL autorisée ou fichier serveur), puis lance l'analyse en tâche de fond."""
    ing = VideoIngestionService(db)
    opts = body.options.model_dump(exclude_none=True)
    try:
        if body.source.startswith(("http://", "https://")):
            # vérifications immédiates ; le téléchargement (parfois long) part en tâche de fond
            video, existed = ing.prepare_url(body.source, body.rights_confirmed, body.rights_note, body.title, opts,
                                             start=body.start, end=body.end)
            if not existed:
                job = queue().enqueue(db, "import", video.id, {"then": body.auto})
                return {"video": video_out(video, db), "already_imported": False,
                        "job": {"id": job.id, "type": job.type, "status": job.status}}
        else:
            video, existed = ing.import_file(body.source, body.title, body.rights_confirmed, body.rights_note, opts,
                                             start=body.start, end=body.end)
    except IngestionError as exc:
        raise HTTPException(400, str(exc)) from exc
    if existed and opts:
        video.options = {**(video.options or {}), **opts}
        db.add(video)
        db.commit()
    job = _enqueue_auto(db, video, body.auto)
    return {"video": video_out(video, db), "already_imported": existed, "job": job}


def _upload_options(profile: Optional[str], options: Optional[str]) -> Optional[dict]:
    import json

    from app.api.schemas import VideoOptions

    data: dict = {}
    if options:
        try:
            data = VideoOptions(**json.loads(options)).model_dump(exclude_none=True)
        except Exception as exc:
            raise HTTPException(400, f"Options invalides : {exc}") from exc
    if profile:
        data["profile"] = profile
    return data or None


@app.post("/videos/upload", status_code=201, tags=["vidéos"])
def upload_video(file: UploadFile = File(...), rights_confirmed: bool = Form(False), title: Optional[str] = Form(None),
                 auto: str = Form("analyze"), start: Optional[str] = Form(None), end: Optional[str] = Form(None),
                 profile: Optional[str] = Form(None), options: Optional[str] = Form(None),
                 db: Session = Depends(get_session)):
    """Envoi d'un fichier vidéo depuis le navigateur."""
    uploads = get_settings().data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    safe = Path(file.filename or "video.mp4").name
    dst = uploads / safe
    with dst.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        video, existed = VideoIngestionService(db).import_file(dst, title or Path(safe).stem, rights_confirmed,
                                                               options=_upload_options(profile, options),
                                                               start=start or None, end=end or None)
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


@app.get("/videos/{video_id}/source", tags=["vidéos"])
def video_source(video_id: str, db: Session = Depends(get_session)):
    """Vidéo source (pour la prévisualisation des moments ; supporte les requêtes Range)."""
    v = get_or_404(db, Video, video_id)
    if not v.file_path or not Path(v.file_path).exists():
        raise HTTPException(404, "Fichier source absent")
    return FileResponse(v.file_path, media_type="video/mp4")


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


@app.post("/videos/{video_id}/compilation", tags=["vidéos"])
def create_compilation(video_id: str, request: Request, count: int = Query(5, ge=2, le=15),
                       piece_seconds: float = Query(10, ge=4, le=30), sync: bool = False,
                       db: Session = Depends(get_session)):
    """Une vidéo avec les N meilleurs passages courts mis bout à bout."""
    get_or_404(db, Video, video_id)
    if sync:
        try:
            return {"clip": clip_out(pipeline(request, db).generate_compilation(video_id, count, piece_seconds))}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    job = queue().enqueue(db, "compilation", video_id, {"count": count, "piece_seconds": piece_seconds}, dedupe=False)
    return {"job": job.model_dump()}


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
def list_clips(status: Optional[str] = None, video_id: Optional[str] = None,
               group: bool = Query(True, description="Une seule carte par moment (version principale)"),
               db: Session = Depends(get_session)):
    """Clips récents. Par défaut : sans les clips écartés (sauf status=rejected), une carte par moment."""
    q = select(Clip).order_by(Clip.created_at.desc())
    if status:   # un ou plusieurs statuts : ?status=ready,approved
        q = q.where(Clip.status.in_([x.strip() for x in status.split(",") if x.strip()]))
    else:
        q = q.where(Clip.status != ClipStatus.REJECTED)
    if video_id:
        q = q.where(Clip.video_id == video_id)
    clips = db.exec(q).all()
    expiry = _expiry_lookup(db)
    if not group:
        return [{**clip_out(c), "expires_at": expiry(c.video_id)} for c in clips]
    rank = {"main": 0, "compilation": 0, "alt": 1}
    by_key: dict[str, list[Clip]] = {}
    for c in clips:   # la compilation est un clip à part entière ; les variantes d'un moment sont regroupées
        key = c.id if c.variant == "compilation" else c.highlight_id
        by_key.setdefault(key, []).append(c)
    out = []
    for group_clips in by_key.values():
        best = sorted(group_clips, key=lambda c: rank.get(c.variant, 2))[0]
        d = clip_out(best)
        d["expires_at"] = expiry(best.video_id)
        d["other_variants"] = [c.id for c in group_clips if c.id != best.id]
        out.append(d)
    out.sort(key=lambda d: d["created_at"], reverse=True)
    return out


@app.delete("/clips/{clip_id}", tags=["clips"])
def delete_clip(clip_id: str, db: Session = Depends(get_session)):
    """Supprime le clip et ses fichiers (libère de la place sur le serveur)."""
    c = get_or_404(db, Clip, clip_id)
    if c.file_path:
        folder = Path(c.file_path).parent
        out_root = get_settings().output_dir.resolve()
        if folder.resolve() != out_root and out_root in folder.resolve().parents:
            shutil.rmtree(folder, ignore_errors=True)
    db.delete(c)
    db.commit()
    return {"deleted": clip_id}


@app.get("/clips/{clip_id}", tags=["clips"])
def get_clip(clip_id: str, db: Session = Depends(get_session)):
    c = get_or_404(db, Clip, clip_id)
    d = clip_out(c)
    d["expires_at"] = _expiry_lookup(db)(c.video_id)
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


@app.get("/clips/{clip_id}/download", tags=["clips"])
def download_clip(clip_id: str, db: Session = Depends(get_session)):
    """Le MP4 en pièce jointe (Content-Disposition: attachment) : téléchargement direct, y compris sur iPhone."""
    from app.services.pipeline import slugify

    c = get_or_404(db, Clip, clip_id)
    if not c.file_path or not Path(c.file_path).exists():
        raise HTTPException(404, "Clip pas encore rendu")
    import unicodedata

    base = unicodedata.normalize("NFKD", c.title or c.hook.get("text") or "clip").encode("ascii", "ignore").decode()
    name = f"{slugify(base) or 'clip'}.mp4"   # nom ASCII : compris par tous les navigateurs
    return FileResponse(c.file_path, media_type="video/mp4", filename=name)


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
