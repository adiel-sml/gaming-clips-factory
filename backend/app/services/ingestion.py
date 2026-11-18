"""VideoIngestionService : import d'un fichier local ou d'une URL autorisée.

Garde-fous légaux :
  - confirmation explicite des droits d'utilisation (configurable) ;
  - domaines autorisés uniquement ;
  - téléchargement via yt-dlp *sans* cookies, sans contournement de DRM ni de
    vérification d'âge/connexion : si la plateforme refuse, on s'arrête et on
    l'explique. Pour vos propres vidéos, préférez l'export depuis YouTube Studio.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urlparse

from sqlmodel import Session, select

from app.core.config import Settings, get_settings
from app.core.utils import file_fingerprint, get_logger, utcnow
from app.models.entities import Video, VideoStatus
from app.video import ffmpeg

log = get_logger("ingestion")


class IngestionError(ValueError):
    pass


class VideoIngestionService:
    def __init__(self, session: Session, settings: Settings | None = None):
        self.db = session
        self.s = settings or get_settings()

    def video_dir(self, video_id: str) -> Path:
        d = self.s.data_dir / "videos" / video_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _check_rights(self, rights_confirmed: bool) -> None:
        if self.s.get("ingestion.require_rights_confirmation", True) and not rights_confirmed:
            raise IngestionError(
                "Confirmez que vous avez le droit d'utiliser cette vidéo (votre contenu, licence, ou autorisation "
                "du créateur) : option --confirm-rights / champ rights_confirmed=true.")

    def _finalize(self, video: Video, path: Path) -> Video:
        info = ffmpeg.probe(path)
        max_d = float(self.s.get("ingestion.max_video_duration", 14400))
        if info.duration > max_d:
            raise IngestionError(f"Vidéo trop longue ({info.duration:.0f}s > {max_d:.0f}s).")
        video.file_path = str(path)
        video.duration, video.width, video.height = info.duration, info.width, info.height
        video.fps, video.has_audio = info.fps, info.has_audio
        video.status = VideoStatus.IMPORTED
        video.updated_at = utcnow()
        self.db.add(video)
        self.db.commit()
        self.db.refresh(video)
        return video

    # ------------------------------------------------------------------ fichier local
    def import_file(self, path: str | Path, title: str | None = None, rights_confirmed: bool = False,
                    rights_note: str | None = None, options: dict | None = None) -> tuple[Video, bool]:
        """Retourne (vidéo, déjà_existante)."""
        self._check_rights(rights_confirmed)
        src = Path(path).expanduser().resolve()
        if not src.is_file():
            raise IngestionError(f"Fichier introuvable : {src}")
        fp = file_fingerprint(src)
        existing = self.db.exec(select(Video).where(Video.file_hash == fp)).first()
        if existing:
            return existing, True
        video = Video(source_type="file", title=title or src.stem, file_hash=fp, rights_note=rights_note,
                      options=options or {})
        dst = self.video_dir(video.id) / f"source{src.suffix.lower()}"
        try:
            dst.symlink_to(src)          # pas de copie de fichiers de plusieurs Go
        except OSError:
            shutil.copy2(src, dst)
        return self._finalize(video, dst), False

    # ------------------------------------------------------------------ URL
    def import_url(self, url: str, rights_confirmed: bool = False, rights_note: str | None = None,
                   title: str | None = None, options: dict | None = None) -> tuple[Video, bool]:
        self._check_rights(rights_confirmed)
        host = (urlparse(url).hostname or "").lower()
        allowed = self.s.get("ingestion.allowed_url_domains", []) or []
        if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
            raise IngestionError(f"Domaine non autorisé : {host}. Autorisés : {', '.join(allowed)}")
        existing = self.db.exec(select(Video).where(Video.source_url == url)).first()
        if existing and existing.status != VideoStatus.FAILED and existing.file_path:
            return existing, True
        video = existing or Video(source_type="url", source_url=url, title=title, rights_note=rights_note,
                                  options=options or {})
        path = self._download(url, self.video_dir(video.id), video)
        video.file_hash = file_fingerprint(path)
        return self._finalize(video, path), False

    def _download(self, url: str, out_dir: Path, video: Video) -> Path:
        try:
            import yt_dlp
        except ImportError as exc:
            raise IngestionError("yt-dlp n'est pas installé (pip install yt-dlp).") from exc
        h = int(self.s.get("ingestion.max_download_height", 1080))
        opts = {
            "format": f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/bv*[height<={h}]+ba/b",
            "outtmpl": str(out_dir / "source.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:  # yt_dlp.utils.DownloadError & co
            raise IngestionError(
                f"Téléchargement impossible : {str(exc)[:300]}. Le système ne contourne pas les protections "
                "des plateformes ; importez plutôt le fichier source que vous détenez.") from exc
        video.title = video.title or info.get("title")
        candidates = sorted(out_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
        candidates = [p for p in candidates if p.suffix not in (".part", ".ytdl", ".json")]
        if not candidates:
            raise IngestionError("Téléchargement terminé mais fichier introuvable.")
        return candidates[0]
