"""CLI GamingClips Factory.

    clips import <fichier|URL> --confirm-rights
    clips analyze <video_id|fichier|URL>
    clips highlights <video_id>
    clips generate <highlight_id>
    clips pipeline <video_id|fichier|URL> [--top 5]
    clips batch videos.txt [--now]
    clips export <clip_id>
    clips status
    clips worker
    clips serve
Équivalent : python -m app <commande> (depuis le dossier backend/).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from sqlmodel import func, select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.hardware import detect_hardware, pick_video_encoder, pick_whisper_model
from app.core.utils import fmt_ts, setup_logging
from app.models.entities import Clip, Highlight, Job, Video

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_enable=False, help="GamingClips Factory — clips gaming automatiques")


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Logs détaillés")):
    s = get_settings()
    setup_logging("DEBUG" if verbose else "INFO", s.data_dir / "logs" / "gcf.log")


def _out(data) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _resolve_video(session, ref: str, confirm_rights: bool, title: str | None = None, options: dict | None = None) -> Video:
    """Accepte un id de vidéo, un chemin de fichier ou une URL."""
    from app.services.ingestion import VideoIngestionService

    video = session.get(Video, ref)
    if video:
        return video
    ing = VideoIngestionService(session)
    if ref.startswith(("http://", "https://")):
        video, existed = ing.import_url(ref, rights_confirmed=confirm_rights, title=title, options=options)
    else:
        video, existed = ing.import_file(ref, title=title, rights_confirmed=confirm_rights, options=options)
    typer.echo(f"{'Déjà importée' if existed else 'Importée'} : {video.id} ({video.title}, {video.duration:.0f}s)")
    return video


def _options(min_d, max_d, clips, lang, style, aggr, layout) -> dict:
    opts = {"min_clip_duration": min_d, "max_clip_duration": max_d, "clips_per_video": clips, "language": lang,
            "subtitle_style": style, "aggressiveness": aggr, "layout": layout}
    return {k: v for k, v in opts.items() if v is not None}


ConfirmOpt = typer.Option(False, "--confirm-rights", help="Je confirme avoir le droit d'utiliser cette vidéo")


@app.command("hw")
def hardware():
    """Matériel détecté et modèles choisis automatiquement."""
    hw = detect_hardware()
    s = get_settings()
    model, device, compute = pick_whisper_model(s.get("transcription.model", "auto"), hw)
    _out({**hw.to_dict(), "whisper": {"model": model, "device": device, "compute_type": compute},
          "video_encoder": pick_video_encoder(s.get("output.video_codec", "auto"), hw)})


@app.command("import")
def import_(source: str, confirm_rights: bool = ConfirmOpt, title: Optional[str] = None,
            rights_note: Optional[str] = typer.Option(None, help="Origine des droits (licence, accord…)")):
    """Importe un fichier local ou une URL autorisée."""
    from app.services.ingestion import VideoIngestionService

    with session_scope() as db:
        ing = VideoIngestionService(db)
        if source.startswith(("http://", "https://")):
            v, existed = ing.import_url(source, confirm_rights, rights_note, title)
        else:
            v, existed = ing.import_file(source, title, confirm_rights, rights_note)
        _out({"id": v.id, "title": v.title, "duration": v.duration, "resolution": f"{v.width}x{v.height}",
              "fps": v.fps, "already_imported": existed})


@app.command()
def analyze(video: str, confirm_rights: bool = ConfirmOpt, force: bool = False,
            clips: Optional[int] = typer.Option(None, help="Nombre de moments à retenir"),
            min_duration: Optional[float] = None, max_duration: Optional[float] = None,
            language: Optional[str] = None, aggressiveness: Optional[str] = None):
    """Analyse une vidéo (transcription + détection des moments)."""
    from app.services.pipeline import PipelineService

    with session_scope() as db:
        v = _resolve_video(db, video, confirm_rights, options=_options(min_duration, max_duration, clips, language, None, aggressiveness, None))
        hls = PipelineService(db).analyze(v.id, force=force)
        _print_highlights(hls)


def _print_highlights(hls: list[Highlight]) -> None:
    for h in hls:
        typer.echo(f"#{h.rank:<2} {h.id}  score={h.score:5.1f}  {fmt_ts(h.start)} → {fmt_ts(h.end)} "
                   f"({h.duration:.0f}s)  [{h.category}] conf={h.confidence:.2f}")
        typer.echo(f"     {h.reason}")
        if h.transcript:
            typer.echo(f"     « {h.transcript[:140]}{'…' if len(h.transcript) > 140 else ''} »")


@app.command()
def highlights(video_id: str, as_json: bool = typer.Option(False, "--json")):
    """Liste les meilleurs moments d'une vidéo analysée."""
    with session_scope() as db:
        hls = db.exec(select(Highlight).where(Highlight.video_id == video_id, Highlight.status != "duplicate")
                      .order_by(Highlight.rank)).all()
        if as_json:
            _out([{"id": h.id, "start": fmt_ts(h.start), "end": fmt_ts(h.end), "duration": h.duration,
                   "score": h.score, "category": h.category, "reason": h.reason, "confidence": h.confidence,
                   "transcript": h.transcript} for h in hls])
        else:
            _print_highlights(hls)


@app.command()
def generate(highlight_id: str, variant: list[str] = typer.Option(None, help="Variantes (main, alt…)")):
    """Génère le(s) clip(s) d'un moment."""
    from app.services.pipeline import PipelineService

    with session_scope() as db:
        clips = PipelineService(db).generate(highlight_id, variant or None)
        for c in clips:
            _print_clip(c)


def _print_clip(c: Clip) -> None:
    typer.echo(f"{c.id} [{c.variant}] {c.status}  {c.file_path}")
    if c.hook:
        typer.echo(f"   hook : {c.hook.get('text')}")
    for r in (c.qc or {}).get("reasons", []):
        typer.echo(f"   ⚠ {r}")


@app.command()
def pipeline(video: str, confirm_rights: bool = ConfirmOpt, top: Optional[int] = typer.Option(None, help="Clips à générer"),
             force: bool = False, clips: Optional[int] = None, min_duration: Optional[float] = None,
             max_duration: Optional[float] = None, language: Optional[str] = None,
             style: Optional[str] = typer.Option(None, help="Preset de sous-titres"),
             aggressiveness: Optional[str] = None, layout: Optional[str] = None):
    """Tout le pipeline : import → analyse → clips → QC → export."""
    from app.services.pipeline import PipelineService

    with session_scope() as db:
        v = _resolve_video(db, video, confirm_rights,
                           options=_options(min_duration, max_duration, clips, language, style, aggressiveness, layout))
        result = PipelineService(db).run_full(v.id, top_n=top, force=force)
        for cid in result["clips"]:
            _print_clip(db.get(Clip, cid))
        _out(result)


@app.command()
def batch(file: Path, confirm_rights: bool = ConfirmOpt, now: bool = typer.Option(False, help="Traiter immédiatement (sinon : file d'attente)"),
          top: Optional[int] = None):
    """Traite une liste de vidéos (une URL ou un chemin par ligne, # = commentaire)."""
    from app.services.pipeline import PipelineService
    from app.workers.queue import JobQueue

    lines = [ln.strip() for ln in file.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    summary = []
    q = JobQueue()
    from app.core.config import PROJECT_ROOT

    def locate(ref: str) -> str:
        if ref.startswith(("http://", "https://")) or Path(ref).is_absolute():
            return ref
        for base in (Path.cwd(), file.resolve().parent, PROJECT_ROOT):
            if (base / ref).exists():
                return str(base / ref)
        return ref

    for ref in map(locate, lines):
        with session_scope() as db:
            try:
                v = _resolve_video(db, ref, confirm_rights)
                if v.status == "analyzed" and db.exec(select(Clip).where(Clip.video_id == v.id)).first():
                    summary.append({"source": ref, "video_id": v.id, "status": "déjà traitée"})
                    continue
                if now:
                    res = PipelineService(db).run_full(v.id, top_n=top)
                    summary.append({"source": ref, "video_id": v.id, "status": "ok", "clips": len(res["clips"])})
                else:
                    job = q.enqueue(db, "pipeline", v.id, {"top_n": top})
                    summary.append({"source": ref, "video_id": v.id, "status": "en file", "job": job.id})
            except Exception as exc:  # une vidéo en erreur ne bloque pas le lot
                summary.append({"source": ref, "status": "erreur", "error": str(exc)[:300]})
    _out(summary)
    if not now:
        typer.echo("Lancez `clips worker` pour traiter la file.")


@app.command()
def export(clip_id: str, force: bool = typer.Option(False, help="Exporter même si needs_review")):
    """Exporte un clip prêt à publier dans output/ready/."""
    from app.services.pipeline import PipelineService

    with session_scope() as db:
        path = PipelineService(db).export(clip_id, force=force)
        typer.echo(f"Exporté : {path}")


@app.command("clips")
def list_clips(video_id: Optional[str] = None, status: Optional[str] = None):
    """Liste les clips générés."""
    with session_scope() as db:
        q = select(Clip).order_by(Clip.created_at.desc())
        if video_id:
            q = q.where(Clip.video_id == video_id)
        if status:
            q = q.where(Clip.status == status)
        for c in db.exec(q).all():
            _print_clip(c)


@app.command()
def status():
    """Vue d'ensemble : vidéos, clips, file d'attente."""
    with session_scope() as db:
        def count(model, *where):
            q = select(func.count()).select_from(model)
            for w in where:
                q = q.where(w)
            return db.exec(q).one()

        _out({
            "videos": count(Video),
            "videos_by_status": dict(db.exec(select(Video.status, func.count()).group_by(Video.status)).all()),
            "highlights": count(Highlight, Highlight.status != "duplicate"),
            "clips_by_status": dict(db.exec(select(Clip.status, func.count()).group_by(Clip.status)).all()),
            "jobs_by_status": dict(db.exec(select(Job.status, func.count()).group_by(Job.status)).all()),
            "failed_videos": [{"id": v.id, "error": (v.error or "")[:200]}
                              for v in db.exec(select(Video).where(Video.status == "failed")).all()],
        })


@app.command()
def worker(once: bool = typer.Option(False, help="Vider la file puis s'arrêter")):
    """Traite la file d'attente (tâches de l'API ou de `clips batch`)."""
    from app.workers.queue import JobQueue

    n = JobQueue().run_worker(once=once)
    typer.echo(f"{n} tâche(s) traitée(s)")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
    """Lance l'API FastAPI (docs : http://localhost:8000/docs)."""
    import uvicorn

    uvicorn.run("app.api.main:app", host=host, port=port, reload=reload)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
