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

import numpy as np
from sqlmodel import Session, select

from app.captions.hooks import HookGenerationService
from app.captions.llm import LLMProvider, build_llm, refine_moments
from app.captions.metadata import MetadataGenerationService
from app.clips.composition import VideoCompositionService
from app.clips.dedup import contains, time_iou
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
    """Réglages effectifs d'une vidéo : config + profil de contenu + options choisies à l'import."""
    from app.core.config import _deep_merge

    base = base or get_settings()
    opts = video.options or {}
    profile_name = opts.get("profile") or base.get("content.profile", "stream") or "stream"
    profile = base.profiles().get(profile_name) or {}
    overrides: dict = _deep_merge(profile.get("overrides") or {}, {"content": {"profile": profile_name}})
    explicit: dict = {}
    for opt, path in OPTION_MAP.items():
        value = opts.get(opt)
        if value not in (None, ""):
            node = explicit
            *parents, leaf = path.split(".")
            for p in parents:
                node = node.setdefault(p, {})
            node[leaf] = value
    overrides = _deep_merge(overrides, explicit)   # les choix explicites de l'utilisateur priment
    return Settings(_deep_merge(dict(base), overrides))


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
        last = {"t": 0.0, "pct": -1.0}

        def report(step: str, pct: float | None = None) -> None:
            """Progression visible dans l'interface (écrite au plus toutes les 5 s)."""
            now = time.time()
            if pct is not None and now - last["t"] < 5 and pct - last["pct"] < 0.05 and pct < 1:
                return
            last["t"], last["pct"] = now, pct if pct is not None else -1.0
            video.stats = {**(video.stats or {}), "progress": {"step": step, "pct": None if pct is None else round(pct * 100)}}
            self.db.add(video)
            self.db.commit()
            log.info("%s : %s%s", video.id, step, "" if pct is None else f" {pct:.0%}")

        try:
            src, wd = Path(video.file_path), self.work_dir(video)
            t = time.time()
            report("Analyse de l'image et du son", 0.0)
            analysis = VideoAnalysisService(s).analyze(src, wd, force=force,
                                                       progress=lambda p: report("Analyse de l'image et du son", p))
            timings["video_analysis"] = round(time.time() - t, 2)

            t = time.time()
            if video.has_audio:
                audio = VideoAnalysisService(s).audio_path(src, wd)
                report("Transcription", 0.0)
                transcript = TranscriptionService(self.transcriber, s).transcribe(
                    audio, wd, force=force, progress=lambda p: report("Transcription", p))
            else:
                transcript = Transcript(language="unknown", segments=[])
            timings["transcription"] = round(time.time() - t, 2)
            video.language = transcript.language

            games = GameDetector(s)
            video.game = games.detect(video.title, transcript.words)

            t = time.time()
            detector = HighlightDetectionService(s, games)
            selector = ClipSelectionService(s)
            candidates: list[MomentCandidate] = []
            picker_mode = str(s.get("scoring.picker", "auto")).lower()
            used = "rules"
            if self.llm.available and picker_mode in ("auto", "llm"):
                # méthode « transcription annotée » : l'IA lit tout et note sévèrement (0-10)
                from app.scoring.llm_picker import LLMMomentPicker

                report("Choix des moments par l'IA", 0.0)
                try:
                    candidates = LLMMomentPicker(self.llm, detector).pick(
                        analysis, transcript, progress=lambda p: report("Choix des moments par l'IA", p))
                    for c in candidates:
                        c.start, c.end = selector.finalize_bounds(c.start, c.end, transcript, analysis)
                        c.transcript = transcript.text_between(c.start, c.end)
                    used = "llm"
                except Exception as exc:
                    log.warning("Choix par l'IA impossible (%s) : repli sur les règles", exc)
                    candidates = []
            if used == "rules":
                report("Détection des moments")
                candidates = detector.detect(analysis, transcript)
                for c in candidates:
                    c.start, c.end = selector.compute_bounds(c, transcript, analysis)
                    c.transcript = transcript.text_between(c.start, c.end)
                t_llm = time.time()
                self._llm_refine(candidates, detector, s)
                timings["llm"] = round(time.time() - t_llm, 2)
            max_window = (video.options or {}).get("max_video_duration")
            if max_window:
                candidates = [c for c in candidates if c.peak <= float(max_window)]
            timings["highlight_detection"] = round(time.time() - t, 2)
            video.stats = {**(video.stats or {}), "picker": used}

            ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
            kept = selector.dedup.filter_moments(ranked, analysis)
            top = kept[: int(s.get("video.clips_per_video", 10))]
            highlights = self._save_highlights(video, top, [c for c in ranked if c.duplicate_of is not None], kept)

            timings["total"] = round(time.time() - t_all, 2)
            video.status = VideoStatus.ANALYZED
            video.stats = {**{k: v for k, v in (video.stats or {}).items() if k != "progress"},
                           "analysis_timings": timings, "candidates": len(candidates),
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
            match = next((h for h in reusable if time_iou((h.start, h.end), (c.start, c.end)) > 0.3
                          or contains((h.start, h.end), (c.start, c.end))), None)
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
        llm_hook = (hl.signals or {}).get("llm_hook")
        if llm_hook and s.get("hooks.enabled", True):
            from app.captions.hooks import is_grounded

            if is_grounded(llm_hook, hl.transcript, [games.name(video.game) or ""]) and all(h["text"] != llm_hook.upper() for h in hooks):
                hooks = [{**(hooks[0] if hooks else {"duration": float(s.get("hooks.duration", 2.5)),
                                                       "position": "top", "style": "default"}),
                          "text": llm_hook.upper(), "source": "llm"}] + hooks[:2]
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

        def step(label: str) -> None:
            clip.status = ClipStatus.RENDERING
            clip.qc = {"progress": label}
            self.db.add(clip)
            self.db.commit()
            log.info("%s : %s", clip.id, label)

        out_dir = s.output_dir / video.id / clip.id
        out_dir.mkdir(parents=True, exist_ok=True)
        step("Préparation")
        try:
            hook = dict(clip.hook or {})
            segments = (clip.crop or {}).get("segments")
            if segments:      # compilation : plusieurs passages courts mis bout à bout
                result = self._render_compilation(video, s, clip, [tuple(x) for x in segments], hook, out_dir,
                                                  analysis, transcript, step)
                peak_out = 1.0
            else:
                result = self._render_piece(video, s, clip.start, clip.end, clip.layout, clip.crop, clip.subtitle_style,
                                            hook, out_dir, "clip", analysis, transcript, step, trim=True)
                peak_out = result["map"](max(0.0, hl.peak - clip.start))
            mp4 = out_dir / "clip.mp4"
            if result["mp4"] != mp4:
                result["mp4"].replace(mp4)
            (out_dir / "subtitles.srt").write_text(result["srt"], encoding="utf-8")
            thumb = ffmpeg.extract_frame(mp4, min(peak_out, max(0.0, result["duration"] - 0.5)),
                                         out_dir / "thumbnail.jpg", width=540)
            step("Contrôle qualité")
            qc = QualityControlService(s).check(mp4, result["events"], result["plan"],
                                               expect_subtitles=bool(s.get("subtitles.enabled", True)) and result["words"] >= 3,
                                               hook_text=hook.get("text"))
            clip.file_path, clip.thumbnail_path = str(mp4), str(thumb)
            clip.render_seconds = round(result["render_s"], 2)
            clip.qc = {**qc.to_dict(), "plan": result["plan"]}
            clip.status = ClipStatus.READY if qc.passed else ClipStatus.NEEDS_REVIEW
            clip.updated_at = utcnow()
            meta_path = out_dir / "metadata.json"
            write_json(meta_path, self.clip_metadata(clip, hl, video, transcript, result["plan"],
                                                     result["preset"], meta_source))
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

    # ------------------------------------------------------------------ rendu d'un passage
    def _dead_air(self, start: float, end: float, rel_words: list[Word], analysis: AnalysisResult,
                  s: Settings) -> list[tuple[float, float]]:
        """Blancs à couper (jump cuts) : pauses de parole où le son est AUSSI calme.
        Une pause pleine d'action sonore (explosion, public, musique) n'est jamais coupée."""
        if not s.get("clips.trim_silences", True) or len(rel_words) < 2:
            return []
        min_pause = float(s.get("clips.min_pause", 0.7))
        pad = float(s.get("clips.pause_padding", 0.15))
        margin = float(s.get("clips.quiet_margin_db", 8))
        db = np.asarray(analysis.audio_db, dtype=np.float32)
        i0, i1 = analysis.index(start), analysis.index(end)
        if i1 <= i0:
            return []
        clip_med = float(np.median(db[i0:i1 + 1]))
        cuts = []
        for a, b in zip(rel_words, rel_words[1:]):
            g0, g1 = a.end + pad, b.start - pad
            if b.start - a.end >= min_pause and g1 - g0 > 0.2:
                j0, j1 = analysis.index(start + g0), analysis.index(start + g1)
                if float(np.mean(db[j0:j1 + 1])) < clip_med - margin:
                    cuts.append((round(g0, 3), round(g1, 3)))
        min_d = float(s.get("video.min_clip_duration", 12))
        cuts.sort(key=lambda c: c[1] - c[0], reverse=True)   # les plus longs blancs d'abord
        kept, removed = [], 0.0
        for c in cuts:
            if (end - start) - removed - (c[1] - c[0]) >= min_d:
                kept.append(c)
                removed += c[1] - c[0]
        return sorted(kept)

    @staticmethod
    def _time_mapper(cuts: list[tuple[float, float]]):
        """Temps dans le passage d'origine -> temps dans la vidéo après coupe des blancs."""
        def f(t: float) -> float:
            shift = 0.0
            for a, b in cuts:
                if t >= b:
                    shift += b - a
                elif t > a:
                    return round(a - shift, 3)
            return round(t - shift, 3)
        return f

    def _render_piece(self, video: Video, s: Settings, start: float, end: float, layout: str | None, crop: dict | None,
                      style: str | None, hook: dict, out_dir: Path, name: str, analysis: AnalysisResult,
                      transcript: Transcript, step, trim: bool = True) -> dict:
        src, offset, hd_source = Path(video.file_path), 0.0, "file"
        if (video.options or {}).get("remote_hd"):
            src, offset, hd_source = self._hd_source(video, start, end, s, step, name)
        src_info = ffmpeg.probe(src)
        step("Montage vertical, sous-titres et encodage")
        composer = VideoCompositionService(s, face_detector=self.face_detector)
        plan = composer.plan(src, src_info.width, src_info.height, start, end, analysis, layout=layout,
                             crop_override={k: v for k, v in (crop or {}).items() if k != "segments"},
                             time_offset=offset)
        plan.details["hd_source"] = hd_source
        rel_words = [Word(round(w.start - start, 3), round(w.end - start, 3), w.text, w.prob)
                     for w in transcript.words_between(start, end)]
        cuts = self._dead_air(start, end, rel_words, analysis, s) if trim else []
        tmap = self._time_mapper(cuts)
        keep, cursor = [], 0.0
        for a, b in cuts:
            keep.append((cursor, a))
            cursor = b
        keep.append((cursor, end - start))
        out_words = [Word(tmap(w.start), tmap(w.end), w.text, w.prob) for w in rel_words]
        duration = sum(b - a for a, b in keep)
        plan.details["dead_air_removed"] = round((end - start) - duration, 2)
        hook = dict(hook or {})
        if hook:
            hook["y"] = plan.hook_y
        sub_enabled = bool(s.get("subtitles.enabled", True))
        games = GameDetector(s)
        game_kw = (games.games.get(video.game, {}) or {}).get("keywords", []) if video.game else []
        subs = SubtitleService(s).build(out_words if sub_enabled else [], duration, style,
                                        subtitle_y=plan.subtitle_y, hook=hook if hook.get("text") else None,
                                        extra_keywords=game_kw)
        ass_path = out_dir / ("subtitles.ass" if name == "clip" else f"{name}.ass")
        ass_path.write_text(subs.ass, encoding="utf-8")
        mp4 = out_dir / f"{name}.mp4"
        render_s = RenderService(s).render(src, start - offset, end - offset, plan, mp4,
                                           ass_path if (sub_enabled or hook.get("text")) else None,
                                           src_info.has_audio, src_info.fps, keep=keep if cuts else None)
        return {"mp4": mp4, "plan": plan.to_dict(), "events": subs.events, "srt": subs.srt, "preset": subs.preset,
                "words": len(rel_words), "duration": duration, "render_s": render_s, "map": tmap}

    def _render_compilation(self, video: Video, s: Settings, clip: Clip, segments: list[tuple[float, float]],
                            hook: dict, out_dir: Path, analysis: AnalysisResult, transcript: Transcript, step) -> dict:
        from app.subtitles.ass import SubEvent

        pieces, events, srt_parts, total, render_s = [], [], [], 0.0, 0.0
        for i, (a, b) in enumerate(segments):
            r = self._render_piece(video, s, a, b, clip.layout, clip.crop, clip.subtitle_style,
                                   hook if i == 0 else {}, out_dir, f"piece_{i:02d}", analysis, transcript,
                                   lambda label, i=i: step(f"Compilation {i + 1}/{len(segments)} : {label}"), trim=False)
            pieces.append(r)
            events += [SubEvent(e.start + total, e.end + total, e.text, e.width_est, e.words) for e in r["events"]]
            srt_parts.append(r["srt"])
            total += r["duration"]
            render_s += r["render_s"]
        listing = out_dir / "pieces.txt"
        listing.write_text("".join(f"file '{p['mp4'].name}'\n" for p in pieces))
        final = out_dir / "compilation.mp4"
        ffmpeg.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(final)])
        for p in pieces:
            p["mp4"].unlink(missing_ok=True)
        plan = {**pieces[0]["plan"], "segments": [list(x) for x in segments], "pieces": len(pieces)}
        return {"mp4": final, "plan": plan, "events": events, "srt": "\n".join(srt_parts), "preset": pieces[0]["preset"],
                "words": sum(p["words"] for p in pieces), "duration": total, "render_s": render_s, "map": lambda t: t}

    # ------------------------------------------------------------------ compilation
    def generate_compilation(self, video_id: str, count: int = 5, piece_seconds: float = 10.0) -> Clip:
        """Une vidéo avec les meilleurs passages courts mis bout à bout (le meilleur en premier)."""
        video: Video = self._get(Video, video_id)
        s = settings_for(video, self.base)
        analysis, transcript = self.load_cached(video)
        hls = self.db.exec(select(Highlight).where(Highlight.video_id == video_id,
                                                   Highlight.status.not_in(["duplicate", "rejected"]))
                           .order_by(Highlight.score.desc())).all()
        if not hls:
            raise ValueError("Aucun moment détecté : analysez d'abord la vidéo.")
        selector = ClipSelectionService(s)
        segments: list[tuple[float, float]] = []
        for h in hls:
            a = max(h.start, h.peak - piece_seconds * 0.4)
            b = min(h.end, a + piece_seconds)
            a = selector.snap_out_of_words(a, transcript.words, "start")
            b = selector.snap_out_of_words(b, transcript.words, "end")
            if b - a >= 4 and all(b <= x or a >= y for x, y in segments):
                segments.append((round(a, 2), round(b, 2)))
            if len(segments) >= count:
                break
        if len(segments) < 2:
            raise ValueError("Pas assez de moments distincts pour une compilation (2 minimum).")
        lang = "en" if (transcript.language or "").startswith("en") else "fr"
        n = len(segments)
        hook_text = f"TOP {n} MOMENTS 🔥" if lang == "en" else f"LES {n} MEILLEURS MOMENTS 🔥"
        games = GameDetector(s)
        meta = MetadataGenerationService(s, self.llm, games).generate(
            hls[0].category, " ".join(h.transcript for h in hls[:n])[:3000], hook_text, video.game, transcript.language)
        best = hls[0]
        clip = Clip(video_id=video.id, highlight_id=best.id, variant="compilation", start=segments[0][0],
                    end=segments[-1][1], crop={"segments": [list(x) for x in segments]},
                    subtitle_style=s.get("subtitles.style", "gaming"),
                    hook={"text": hook_text, "duration": float(s.get("hooks.duration", 2.5)), "position": "top",
                          "style": "default", "source": "template"},
                    titles=meta["titles"], captions=meta["captions"], hashtags=meta["hashtags"], title=meta["titles"][0])
        self.db.add(clip)
        self.db.commit()
        return self.render_clip(clip.id, analysis=analysis, transcript=transcript, meta_source=meta.get("source"))

    def _hd_source(self, video: Video, start: float, end: float, s: Settings, step, tag: str) -> tuple[Path, float, str]:
        """Passage HD (téléchargé à part), avec délai maximal ; sinon version légère, avec avertissement."""
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FutureTimeout

        from app.services.ingestion import VideoIngestionService

        step("Téléchargement du passage en HD")
        timeout = float(s.get("ingestion.hd_segment_timeout", 300))
        ing = VideoIngestionService(self.db, s)
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            src, offset = pool.submit(ing.fetch_hd_segment, video, start, end).result(timeout=timeout)
            return src, offset, "segment"
        except FutureTimeout:
            log.warning("Passage HD trop long à télécharger pour %s (> %.0f s)", tag, timeout)
            return Path(video.file_path), 0.0, f"fallback: téléchargement HD > {timeout:.0f} s"
        except Exception as exc:
            log.warning("Passage HD indisponible pour %s : %s", tag, exc)
            return Path(video.file_path), 0.0, f"fallback: {str(exc)[:200]}"
        finally:
            pool.shutdown(wait=False)

    @staticmethod
    def clip_metadata(clip: Clip, hl: Highlight, video: Video, transcript: Transcript, plan: dict, preset: str,
                      meta_source: str | None) -> dict:
        return {
            "clip_id": clip.id, "variant": clip.variant, "status": clip.status,
            "video": {"id": video.id, "title": video.title, "source_type": video.source_type,
                      "source_url": video.source_url, "game": video.game, "language": transcript.language,
                      "rights_note": video.rights_note},
            "timing": {"start": clip.start, "end": clip.end, "duration": round(clip.end - clip.start, 2),
                       "start_ts": fmt_ts(clip.start), "end_ts": fmt_ts(clip.end), "peak": hl.peak,
                       # position dans la vidéo d'origine (si seul un extrait a été importé)
                       "source_offset": (video.options or {}).get("section_start", 0),
                       "source_start_ts": fmt_ts((video.options or {}).get("section_start", 0) + clip.start),
                       "source_end_ts": fmt_ts((video.options or {}).get("section_start", 0) + clip.end)},
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
