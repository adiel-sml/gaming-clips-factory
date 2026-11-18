"""Orchestration du pipeline complet.

VideoIngestionService -> VideoAnalysisService -> TranscriptionService -> HighlightDetectionService
-> ClipSelectionService -> VideoCompositionService -> SubtitleService -> HookGenerationService
-> MetadataGenerationService -> QualityControlService -> ExportService

Chaque étape est mise en cache sur disque (analysis.json, transcript.json) : relancer après
une erreur reprend là où le traitement s'était arrêté.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from sqlmodel import Session, select

from app.captions.hooks import HookGenerationService
from app.captions.llm import LLMProvider, build_llm, refine_moments
from app.captions.metadata import MetadataGenerationService
from app.clips.composition import VideoCompositionService
from app.clips.dedup import time_iou
from app.clips.selection import ClipSelectionService
from app.core.config import Settings, get_settings, load_settings
from app.core.utils import fmt_ts, get_logger, utcnow, write_json
from app.models.entities import Clip, ClipStatus, Highlight, Video, VideoStatus
from app.scoring.games import GameDetector
from app.scoring.highlight import HighlightDetectionService, MomentCandidate
from app.services.quality import QualityControlService
from app.services.render import RenderService
from app.subtitles.ass import SubtitleService
from app.transcription.base import Transcriber, Transcript, Word
from app.transcription.service import TranscriptionService
from app.video import ffmpeg
from app.video.analysis import AnalysisResult, VideoAnalysisService

log = get_logger("pipeline")

# options d'import -> clés de configuration
OPTION_MAP = {
    "min_clip_duration": "video.min_clip_duration",
    "max_clip_duration": "video.max_clip_duration",
    "clips_per_video": "video.clips_per_video",
    "language": "transcription.language",
    "subtitle_style": "subtitles.style",
    "aggressiveness": "scoring.aggressiveness",
    "layout": "composition.layout",
}


def settings_for(video: Video, base: Settings | None = None) -> Settings:
    base = base or get_settings()
    overrides: dict = {}
    for opt, path in OPTION_MAP.items():
        value = (video.options or {}).get(opt)
        if value not in (None, ""):
            node = overrides
            *parents, leaf = path.split(".")
            for p in parents:
                node = node.setdefault(p, {})
            node[leaf] = value
    if not overrides:
        return base
    merged = load_settings(overrides=overrides)
    # conserve d'éventuelles surcharges programmatiques de `base` (tests)
    for k, v in base.items():
        if k not in overrides:
            merged[k] = v
        elif isinstance(v, dict):
            merged[k] = {**v, **overrides[k]}
    return merged


def slugify(text: str, max_len: int = 40) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower(), flags=re.UNICODE)
    return re.sub(r"[\s_-]+", "-", text).strip("-")[:max_len] or "clip"


class PipelineService:
    def __init__(self, session: Session, settings: Settings | None = None, transcriber: Transcriber | None = None,
                 llm: LLMProvider | None = None, face_detector=None):
        self.db = session
        self.base = settings or get_settings()
        self.transcriber = transcriber
        self.llm = llm if llm is not None else build_llm(self.base)
        self.face_detector = face_detector

    # ================================================================== helpers
    def work_dir(self, video: Video) -> Path:
        d = self.base.data_dir / "videos" / video.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _get(self, model, obj_id: str):
        obj = self.db.get(model, obj_id)
        if obj is None:
            raise LookupError(f"{model.__name__} introuvable : {obj_id}")
        return obj

    def load_cached(self, video: Video) -> tuple[AnalysisResult, Transcript]:
        s = settings_for(video, self.base)
        wd = self.work_dir(video)
        analysis = VideoAnalysisService(s).analyze(Path(video.file_path), wd)
        audio = VideoAnalysisService(s).audio_path(Path(video.file_path), wd) if video.has_audio else None
        if audio is None:
            transcript = Transcript(language=video.language or "unknown", segments=[])
        else:
            transcript = TranscriptionService(self.transcriber, s).transcribe(audio, wd)
        return analysis, transcript

    # ================================================================== analyse
    def analyze(self, video_id: str, force: bool = False) -> list[Highlight]:
        video: Video = self._get(Video, video_id)
        s = settings_for(video, self.base)
        video.status, video.error = VideoStatus.ANALYZING, None
        self.db.add(video)
        self.db.commit()
        timings: dict[str, float] = {}
        t_all = time.time()
        try:
            src, wd = Path(video.file_path), self.work_dir(video)
            t = time.time()
            analysis = VideoAnalysisService(s).analyze(src, wd, force=force)
            timings["video_analysis"] = round(time.time() - t, 2)

            t = time.time()
            if video.has_audio:
                audio = VideoAnalysisService(s).audio_path(src, wd)
                transcript = TranscriptionService(self.transcriber, s).transcribe(audio, wd, force=force)
            else:
                transcript = Transcript(language="unknown", segments=[])
            timings["transcription"] = round(time.time() - t, 2)
            video.language = transcript.language

            games = GameDetector(s)
            video.game = games.detect(video.title, transcript.words)

            t = time.time()
            detector = HighlightDetectionService(s, games)
            candidates = detector.detect(analysis, transcript)
            max_window = (video.options or {}).get("max_video_duration")
            if max_window:
                candidates = [c for c in candidates if c.peak <= float(max_window)]
            selector = ClipSelectionService(s)
            for c in candidates:
                c.start, c.end = selector.compute_bounds(c, transcript, analysis)
                c.transcript = transcript.text_between(c.start, c.end)
            timings["highlight_detection"] = round(time.time() - t, 2)

            t = time.time()
            self._llm_refine(candidates, detector, s)
            timings["llm"] = round(time.time() - t, 2)

            ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
            kept = selector.dedup.filter_moments(ranked, analysis)
            top = kept[: int(s.get("video.clips_per_video", 10))]
            highlights = self._save_highlights(video, top, [c for c in ranked if c.duplicate_of is not None], kept)

            timings["total"] = round(time.time() - t_all, 2)
            video.status = VideoStatus.ANALYZED
            video.stats = {**(video.stats or {}), "analysis_timings": timings, "candidates": len(candidates),
                           "highlights": len(highlights), "words": len(transcript.words),
                           "transcription_model": transcript.model}
            video.updated_at = utcnow()
            self.db.add(video)
            self.db.commit()
            log.info("Analyse %s : %d candidats, %d moments retenus (%.1fs)", video.id, len(candidates),
                     len(highlights), timings["total"])
            return highlights
        except Exception as exc:
            self.db.rollback()
            video.status, video.error = VideoStatus.FAILED, str(exc)[:2000]
            video.updated_at = utcnow()
            self.db.add(video)
            self.db.commit()
            raise

    def _llm_refine(self, candidates: list[MomentCandidate], detector: HighlightDetectionService, s: Settings) -> None:
        if not self.llm.available or not candidates:
            return
        k = int(s.get("scoring.llm_top_k", 15))
        top = sorted(candidates, key=lambda c: c.score, reverse=True)[:k]
        items = [{"index": i, "start": fmt_ts(c.start), "end": fmt_ts(c.end), "rule_category": c.category,
                  "rule_score": c.score, "signals": c.reason, "transcript": c.transcript[:1500]}
                 for i, c in enumerate(top)]
        results = refine_moments(self.llm, items)
        for i, c in enumerate(top):
            r = results.get(i)
            if not r:
                continue
            c.evidence = {**c.evidence, "rule_reason": c.reason, "rule_score": c.score, "llm": True}
            for key in ("humor", "quotability", "context_independence"):
                c.components[key] = round(0.5 * c.components.get(key, 0) + 0.5 * float(r.get(key, 0)), 3)
            rule = detector.compute_score(c.components)
            if r["category"] == "not_interesting":
                c.score = round(0.6 * rule, 1)
            else:
                c.score = round(0.7 * rule + 0.3 * float(r["interest"]), 1)
                c.category = r["category"]
            c.reason = r.get("reason") or c.reason
            c.confidence = round(min(0.98, c.confidence + 0.05), 2)

    def _save_highlights(self, video: Video, top: list[MomentCandidate], dups: list[MomentCandidate],
                         kept: list[MomentCandidate]) -> list[Highlight]:
        # remplace les anciens moments qui n'ont pas encore de clip
        olds = self.db.exec(select(Highlight).where(Highlight.video_id == video.id)).all()
        with_clips = {c.highlight_id for c in self.db.exec(select(Clip).where(Clip.video_id == video.id)).all()}
        reusable = [h for h in olds if h.id in with_clips]
        for h in olds:
            if h.id not in with_clips:
                self.db.delete(h)
            else:
                h.rank = 0  # reclassé ci-dessous s'il correspond encore à un moment retenu
        self.db.flush()
        saved: list[Highlight] = []
        id_by_index: dict[int, str] = {}
        for rank, c in enumerate(top, 1):
            # un moment qui a déjà des clips est mis à jour plutôt que dupliqué
            match = next((h for h in reusable if time_iou((h.start, h.end), (c.start, c.end)) > 0.5), None)
            fresh = self._to_highlight(video, c, rank)
            if match:
                for field in ("rank", "peak", "score", "category", "reason", "confidence", "components", "signals"):
                    setattr(match, field, getattr(fresh, field))
                reusable.remove(match)
                h = match
            else:
                h = fresh
            id_by_index[kept.index(c)] = h.id
            self.db.add(h)
            saved.append(h)
        for c in dups:  # conservés pour traçabilité, marqués doublons
            h = self._to_highlight(video, c, 0)
            h.status = "duplicate"
            h.duplicate_of = id_by_index.get(c.duplicate_of)
            self.db.add(h)
        self.db.commit()
        return saved

    @staticmethod
    def _to_highlight(video: Video, c: MomentCandidate, rank: int) -> Highlight:
        return Highlight(video_id=video.id, rank=rank, start=c.start, end=c.end, peak=c.peak,
                         duration=round(c.end - c.start, 2), score=c.score, category=c.category, reason=c.reason,
                         confidence=c.confidence, transcript=c.transcript, components=c.components,
                         signals={"onset": c.onset, "reaction_end": c.reaction_end, **c.evidence})

    # ================================================================== génération
    def variant_defs(self, s: Settings, names: list[str] | None = None) -> list[dict]:
        defs = list(s.get("variants.definitions", [{"name": "main"}]))[: int(s.get("variants.count", 1))]
        if names:
            all_defs = {d["name"]: d for d in s.get("variants.definitions", [])}
            defs = [all_defs.get(n, {"name": n}) for n in names]
        return defs or [{"name": "main"}]

    def generate(self, highlight_id: str, variants: list[str] | None = None) -> list[Clip]:
        hl: Highlight = self._get(Highlight, highlight_id)
        video: Video = self._get(Video, hl.video_id)
        s = settings_for(video, self.base)
        analysis, transcript = self.load_cached(video)
        words = [w for w in transcript.words_between(hl.start, hl.end)]
        games = GameDetector(s)
        hooks = HookGenerationService(s, self.llm).generate(
            hl.category, words, hl.transcript, transcript.language, games.name(video.game), hl.reason) \
            if s.get("hooks.enabled", True) else []
        meta = MetadataGenerationService(s, self.llm, games).generate(
            hl.category, hl.transcript, hooks[0]["text"] if hooks else "", video.game, transcript.language, hl.reason)

        clips: list[Clip] = []
        for vdef in self.variant_defs(s, variants):
            existing = self.db.exec(select(Clip).where(Clip.highlight_id == hl.id, Clip.variant == vdef["name"])).first()
            clip = existing or Clip(video_id=video.id, highlight_id=hl.id, variant=vdef["name"], start=hl.start, end=hl.end)
            hook = dict(hooks[min(int(vdef.get("hook_index", 0)), len(hooks) - 1)]) if hooks else {}
            if hook:
                hook["alternatives"] = [h["text"] for h in hooks]
            clip.hook = hook
            clip.layout = vdef.get("layout")
            clip.subtitle_style = vdef.get("subtitle_style") or s.get("subtitles.style", "gaming")
            clip.titles, clip.captions, clip.hashtags = meta["titles"], meta["captions"], meta["hashtags"]
            clip.title = clip.title or (meta["titles"][0] if meta["titles"] else None)
            self.db.add(clip)
            self.db.commit()
            clips.append(self.render_clip(clip.id, analysis=analysis, transcript=transcript, meta_source=meta.get("source")))
        hl.status = "generated"
        self.db.add(hl)
        self.db.commit()
        return clips

    def render_clip(self, clip_id: str, analysis: AnalysisResult | None = None, transcript: Transcript | None = None,
                    meta_source: str | None = None) -> Clip:
        clip: Clip = self._get(Clip, clip_id)
        video: Video = self._get(Video, clip.video_id)
        hl: Highlight = self._get(Highlight, clip.highlight_id)
        s = settings_for(video, self.base)
        if analysis is None or transcript is None:
            analysis, transcript = self.load_cached(video)
        src = Path(video.file_path)
        out_dir = s.output_dir / video.id / clip.id
        out_dir.mkdir(parents=True, exist_ok=True)
        clip.status = ClipStatus.RENDERING
        self.db.add(clip)
        self.db.commit()
        try:
            composer = VideoCompositionService(s, face_detector=self.face_detector)
            plan = composer.plan(src, video.width, video.height, clip.start, clip.end, analysis,
                                 layout=clip.layout, crop_override=clip.crop)
            duration = clip.end - clip.start
            rel_words = [Word(round(w.start - clip.start, 3), round(w.end - clip.start, 3), w.text, w.prob)
                         for w in transcript.words_between(clip.start, clip.end)]
            hook = dict(clip.hook or {})
            if hook:
                hook["y"] = plan.hook_y
            sub_enabled = bool(s.get("subtitles.enabled", True))
            games = GameDetector(s)
            game_kw = (games.games.get(video.game, {}) or {}).get("keywords", []) if video.game else []
            subs = SubtitleService(s).build(rel_words if sub_enabled else [], duration, clip.subtitle_style,
                                            subtitle_y=plan.subtitle_y, hook=hook if hook.get("text") else None,
                                            extra_keywords=game_kw)
            ass_path = out_dir / "subtitles.ass"
            ass_path.write_text(subs.ass, encoding="utf-8")
            (out_dir / "subtitles.srt").write_text(subs.srt, encoding="utf-8")
            needs_ass = sub_enabled or bool(hook.get("text"))

            mp4 = out_dir / "clip.mp4"
            render_s = RenderService(s).render(src, clip.start, clip.end, plan, mp4, ass_path if needs_ass else None,
                                               video.has_audio, video.fps)
            thumb = ffmpeg.extract_frame(mp4, min(max(0.0, hl.peak - clip.start), max(0.0, duration - 0.5)),
                                         out_dir / "thumbnail.jpg", width=540)
            qc = QualityControlService(s).check(mp4, subs.events, plan.to_dict(),
                                               expect_subtitles=sub_enabled and len(rel_words) >= 3,
                                               hook_text=hook.get("text"))
            clip.file_path, clip.thumbnail_path = str(mp4), str(thumb)
            clip.layout = clip.layout or None
            clip.render_seconds = round(render_s, 2)
            clip.qc = {**qc.to_dict(), "plan": plan.to_dict()}
            clip.status = ClipStatus.READY if qc.passed else ClipStatus.NEEDS_REVIEW
            clip.updated_at = utcnow()
            meta_path = out_dir / "metadata.json"
            write_json(meta_path, self.clip_metadata(clip, hl, video, transcript, plan.to_dict(), subs.preset, meta_source))
            clip.metadata_path = str(meta_path)
            self.db.add(clip)
            self.db.commit()
            return clip
        except Exception as exc:
            self.db.rollback()
            clip.status = ClipStatus.FAILED
            clip.qc = {"passed": False, "reasons": [f"Rendu impossible : {str(exc)[:500]}"]}
            self.db.add(clip)
            self.db.commit()
            raise

    @staticmethod
    def clip_metadata(clip: Clip, hl: Highlight, video: Video, transcript: Transcript, plan: dict, preset: str,
                      meta_source: str | None) -> dict:
        return {
            "clip_id": clip.id, "variant": clip.variant, "status": clip.status,
            "video": {"id": video.id, "title": video.title, "source_type": video.source_type,
                      "source_url": video.source_url, "game": video.game, "language": transcript.language,
                      "rights_note": video.rights_note},
            "timing": {"start": clip.start, "end": clip.end, "duration": round(clip.end - clip.start, 2),
                       "start_ts": fmt_ts(clip.start), "end_ts": fmt_ts(clip.end), "peak": hl.peak},
            "highlight": {"id": hl.id, "score": hl.score, "category": hl.category, "reason": hl.reason,
                          "confidence": hl.confidence, "components": hl.components, "signals": hl.signals},
            "transcript": transcript.text_between(clip.start, clip.end),
            "composition": plan, "subtitle_style": preset, "hook": clip.hook,
            "title": clip.title, "titles": clip.titles, "captions": clip.captions, "hashtags": clip.hashtags,
            "copy_source": meta_source, "qc": {k: clip.qc.get(k) for k in ("passed", "reasons", "warnings", "metrics")},
            "files": {"video": clip.file_path, "thumbnail": clip.thumbnail_path},
            "render_seconds": clip.render_seconds, "generated_at": str(utcnow()),
            "performance": clip.performance or {"views": None, "likes": None, "shares": None, "watch_time": None},
        }

    # ================================================================== export
    def export(self, clip_id: str, force: bool = False) -> Path:
        clip: Clip = self._get(Clip, clip_id)
        if clip.status in (ClipStatus.PENDING, ClipStatus.FAILED) or not clip.file_path or not Path(clip.file_path).exists():
            clip = self.render_clip(clip.id)
        if clip.status == ClipStatus.NEEDS_REVIEW and not force:
            raise ValueError("Clip en « needs_review » : " + "; ".join(clip.qc.get("reasons", []))
                             + " — corrigez-le ou exportez avec force=true.")
        if clip.status == ClipStatus.REJECTED:
            raise ValueError("Clip rejeté.")
        ready = self.base.output_dir / "ready"
        ready.mkdir(parents=True, exist_ok=True)
        base = f"{utcnow():%Y%m%d}_{slugify(clip.title or 'clip')}_{clip.id}"
        dst = ready / f"{base}.mp4"
        shutil.copy2(clip.file_path, dst)
        if clip.metadata_path and Path(clip.metadata_path).exists():
            shutil.copy2(clip.metadata_path, ready / f"{base}.json")
        srt = Path(clip.file_path).with_name("subtitles.srt")
        if srt.exists():
            shutil.copy2(srt, ready / f"{base}.srt")
        clip.status = ClipStatus.EXPORTED
        clip.updated_at = utcnow()
        self.db.add(clip)
        self.db.commit()
        return dst

    # ================================================================== tout-en-un
    def run_full(self, video_id: str, top_n: int | None = None, force: bool = False) -> dict:
        highlights = self.analyze(video_id, force=force)
        if top_n:
            highlights = highlights[:top_n]
        clips: list[Clip] = []
        errors: list[str] = []
        for h in highlights:
            try:
                clips += self.generate(h.id)
            except Exception as exc:  # un clip raté ne bloque pas les autres
                log.exception("Génération échouée pour %s", h.id)
                errors.append(f"{h.id}: {exc}")
        return {"video_id": video_id, "highlights": len(highlights), "clips": [c.id for c in clips],
                "ready": sum(c.status == ClipStatus.READY for c in clips),
                "needs_review": sum(c.status == ClipStatus.NEEDS_REVIEW for c in clips), "errors": errors}
