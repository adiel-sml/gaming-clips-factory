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
from app.core.utils import file_fingerprint, fmt_ts, get_logger, parse_time, utcnow
from app.models.entities import Video, VideoStatus
from app.video import ffmpeg

log = get_logger("ingestion")


class IngestionError(ValueError):
    pass


BOT_CHECK_MSG = (
    "YouTube refuse le téléchargement depuis ce serveur (vérification anti-robot : les serveurs cloud sont "
    "souvent bloqués). Le système ne contourne pas cette protection. Solutions : 1) si c'est votre vidéo, "
    "téléchargez-la depuis YouTube Studio puis envoyez le fichier (avec Début/Fin pour un extrait) ; "
    "2) sinon, demandez le fichier au créateur ; 3) ou lancez la CLI sur votre ordinateur."
)


def _download_error(exc: Exception, what: str) -> IngestionError:
    msg = str(exc)
    if "not a bot" in msg or "Sign in to confirm" in msg:
        return IngestionError(BOT_CHECK_MSG)
    if "Private video" in msg or "members-only" in msg.lower():
        return IngestionError("Vidéo privée ou réservée aux membres : impossible de la récupérer.")
    return IngestionError(
        f"{what} : {msg.replace('ERROR: ', '')[:200]}. Le système ne contourne pas les protections des "
        "plateformes ; importez plutôt le fichier source que vous détenez.")


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

    # ------------------------------------------------------------------ plage horaire
    @staticmethod
    def _section(start, end, total: float | None = None) -> tuple[float, float] | None:
        """Valide une plage [début, fin] (secondes ou texte « 6:00:00 », « 6h »…). None = vidéo entière."""
        try:
            s, e = parse_time(start), parse_time(end)
        except ValueError as exc:
            raise IngestionError(str(exc)) from exc
        if s is None and e is None:
            return None
        s = s or 0.0
        if total:
            if s >= total:
                raise IngestionError(f"Le début ({fmt_ts(s)}) dépasse la durée de la vidéo ({fmt_ts(total)}).")
            e = min(e if e is not None else total, total)
        if e is None or e <= s:
            raise IngestionError("La fin de la plage doit être après le début.")
        if e - s < 10:
            raise IngestionError("Plage trop courte (10 s minimum).")
        return round(s, 2), round(e, 2)

    @staticmethod
    def _same_section(video: Video, section: tuple[float, float] | None) -> bool:
        o = video.options or {}
        start = o.get("section_requested_start", o.get("section_start"))
        current = (start, o["section_end"]) if "section_start" in o else None
        return current == section

    def _apply_section(self, video: Video, section: tuple[float, float] | None) -> None:
        if section:
            video.options = {**(video.options or {}), "section_start": section[0], "section_end": section[1]}
            base = video.title or "vidéo"
            video.title = f"{base} [{fmt_ts(section[0])[:8]} → {fmt_ts(section[1])[:8]}]"

    # ------------------------------------------------------------------ fichier local
    def import_file(self, path: str | Path, title: str | None = None, rights_confirmed: bool = False,
                    rights_note: str | None = None, options: dict | None = None,
                    start=None, end=None) -> tuple[Video, bool]:
        """Retourne (vidéo, déjà_existante). `start`/`end` : n'importer qu'un extrait."""
        self._check_rights(rights_confirmed)
        src = Path(path).expanduser().resolve()
        if not src.is_file():
            raise IngestionError(f"Fichier introuvable : {src}")
        section = self._section(start, end, ffmpeg.probe(src).duration) if (start or end) else None
        fp = file_fingerprint(src)
        for existing in self.db.exec(select(Video).where(Video.file_hash == fp)).all():
            if self._same_section(existing, section):
                return existing, True
        video = Video(source_type="file", title=title or src.stem, file_hash=fp, rights_note=rights_note,
                      options=options or {})
        self._apply_section(video, section)
        if section:
            # la coupe sans ré-encodage commence à l'image-clé précédente : on enregistre ce vrai début
            # pour que les heures « dans la vidéo d'origine » restent exactes
            kf = ffmpeg.keyframe_before(src, section[0])
            video.options = {**video.options, "section_start": round(kf, 4), "section_requested_start": section[0]}
            section = (kf, section[1])
            # extrait sans ré-encodage (rapide) ; précision au keyframe près
            dst = self.video_dir(video.id) / "source.mp4"
            # +5 ms : garantit que FFmpeg retient cette image-clé et pas la précédente (arrondis)
            ffmpeg.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{section[0] + 0.005:.3f}", "-to", f"{section[1]:.3f}",
                        "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                        "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(dst)])
            return self._finalize(video, dst), False
        dst = self.video_dir(video.id) / f"source{src.suffix.lower()}"
        try:
            dst.symlink_to(src)          # pas de copie de fichiers de plusieurs Go
        except OSError:
            shutil.copy2(src, dst)
        return self._finalize(video, dst), False

    # ------------------------------------------------------------------ URL
    def prepare_url(self, url: str, rights_confirmed: bool = False, rights_note: str | None = None,
                    title: str | None = None, options: dict | None = None, start=None, end=None) -> tuple[Video, bool]:
        """Étape rapide (quelques secondes) : vérifications + création de la vidéo en « downloading ».
        Le téléchargement lui-même se fait ensuite avec `download_url` (tâche de fond)."""
        self._check_rights(rights_confirmed)
        host = (urlparse(url).hostname or "").lower()
        allowed = self.s.get("ingestion.allowed_url_domains", []) or []
        if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
            raise IngestionError(f"Domaine non autorisé : {host}. Autorisés : {', '.join(allowed)}")
        info = self._probe_url(url)
        total = float(info.get("duration") or 0) or None
        section = self._section(start, end, total) if (start or end) else None
        max_d = float(self.s.get("ingestion.max_video_duration", 14400))
        length = (section[1] - section[0]) if section else total
        if length and length > max_d:
            raise IngestionError(
                f"Vidéo trop longue ({fmt_ts(length)[:8]}, max {fmt_ts(max_d)[:8]}). Indiquez une plage "
                "début/fin, par ex. de 6:00:00 à 7:00:00.")
        candidates = self.db.exec(select(Video).where(Video.source_url == url)).all()
        existing = next((v for v in candidates if self._same_section(v, section)), None)
        if existing and existing.status not in (VideoStatus.FAILED,) and (existing.file_path or existing.status == VideoStatus.DOWNLOADING):
            return existing, True
        video = existing or Video(source_type="url", source_url=url, title=title or info.get("title"),
                                  rights_note=rights_note, options=options or {})
        if not existing:
            self._apply_section(video, section)
        video.status, video.error = VideoStatus.DOWNLOADING, None
        video.updated_at = utcnow()
        self.db.add(video)
        self.db.commit()
        self.db.refresh(video)
        return video, False

    def download_url(self, video: Video) -> Video:
        """Étape longue : télécharge (seulement la plage demandée) puis finalise la vidéo."""
        o = video.options or {}
        section = (o.get("section_requested_start", o["section_start"]), o["section_end"]) if "section_start" in o else None
        proxy = self.s.get("ingestion.url_mode", "proxy") == "proxy"
        try:
            path = self._download(video.source_url, self.video_dir(video.id), video, section,
                                  quality="proxy" if proxy else "full")
            video.file_hash = file_fingerprint(path)
            if proxy:
                # analyse et aperçu sur la version légère ; la HD n'est récupérée qu'au rendu, clip par clip
                video.options = {**(video.options or {}), "remote_hd": True,
                                 "proxy_height": int(self.s.get("ingestion.proxy_height", 360))}
            return self._finalize(video, path)
        except Exception as exc:
            self.db.rollback()
            video.status, video.error = VideoStatus.FAILED, str(exc)[:2000]
            video.updated_at = utcnow()
            self.db.add(video)
            self.db.commit()
            raise

    def import_url(self, url: str, rights_confirmed: bool = False, rights_note: str | None = None,
                   title: str | None = None, options: dict | None = None, start=None, end=None) -> tuple[Video, bool]:
        """Import complet bloquant (CLI)."""
        video, existed = self.prepare_url(url, rights_confirmed, rights_note, title, options, start, end)
        if existed and video.file_path:
            return video, True
        return self.download_url(video), False

    def _probe_url(self, url: str) -> dict:
        """Métadonnées (titre, durée) sans rien télécharger."""
        try:
            import yt_dlp
        except ImportError as exc:
            raise IngestionError("yt-dlp n'est pas installé (pip install yt-dlp).") from exc
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
                return ydl.extract_info(url, download=False) or {}
        except Exception as exc:
            raise _download_error(exc, "Vidéo inaccessible") from exc

    def _download(self, url: str, out_dir: Path, video: Video, section: tuple[float, float] | None = None,
                  quality: str = "full", name: str = "source") -> Path:
        """yt-dlp sans contournement. quality="proxy" : petite résolution (analyse) ; "full" : HD (rendu)."""
        try:
            import yt_dlp
        except ImportError as exc:
            raise IngestionError("yt-dlp n'est pas installé (pip install yt-dlp).") from exc
        if quality == "proxy":
            h = int(self.s.get("ingestion.proxy_height", 360))
            fmt = f"bv*[height<={h}]+ba/b[height<={h}]/wv*+ba/w"
        else:
            h = int(self.s.get("ingestion.max_download_height", 1080))
            fmt = f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/bv*[height<={h}]+ba/b"
        out_dir.mkdir(parents=True, exist_ok=True)
        opts = {
            "format": fmt,
            "outtmpl": str(out_dir / f"{name}.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
        }
        if section:
            # ne télécharge que la plage demandée (1 h au lieu de 10 h, ou 1 min pour un clip)
            from yt_dlp.utils import download_range_func

            opts["download_ranges"] = download_range_func(None, [section])
            opts["force_keyframes_at_cuts"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:  # yt_dlp.utils.DownloadError & co
            raise _download_error(exc, "Téléchargement impossible") from exc
        video.title = video.title or info.get("title")
        candidates = sorted(out_dir.glob(f"{name}.*"), key=lambda p: p.stat().st_size, reverse=True)
        candidates = [p for p in candidates if p.suffix not in (".part", ".ytdl", ".json")]
        if not candidates:
            raise IngestionError("Téléchargement terminé mais fichier introuvable.")
        return candidates[0]

    # ------------------------------------------------------------------ HD à la demande
    SEGMENT_PAD = 5.0

    def fetch_hd_segment(self, video: Video, start: float, end: float) -> tuple[Path, float]:
        """Télécharge (ou réutilise) le passage HD couvrant [start, end] (temps de la vidéo importée).

        Retourne (fichier, début du fichier dans la timeline de la vidéo). Une marge de 5 s de chaque côté
        permet de petits ajustements dans l'éditeur sans retélécharger."""
        seg_dir = self.video_dir(video.id) / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        for f in seg_dir.glob("hd_*.mp4"):          # cache : un passage déjà téléchargé qui couvre le clip
            try:
                a, b = (float(x) for x in f.stem[3:].split("_"))
            except ValueError:
                continue
            if a <= start + 0.05 and end - 0.05 <= b:
                return f, a
        seg_start = max(0.0, start - self.SEGMENT_PAD)
        seg_end = min(video.duration or end + self.SEGMENT_PAD, end + self.SEGMENT_PAD)
        o = video.options or {}
        origin = float(o.get("section_requested_start", o.get("section_start", 0.0)))
        name = f"hd_{seg_start:.2f}_{seg_end:.2f}"
        path = self._download(video.source_url, seg_dir, video, (origin + seg_start, origin + seg_end),
                              quality="full", name=name)
        if path.suffix != ".mp4":
            final = path.with_suffix(".mp4")
            path.rename(final)
            path = final
        return path, seg_start
