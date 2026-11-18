"""VideoCompositionService : choix du cadrage 9:16 et construction du filtergraph FFmpeg.

Stratégies :
  gameplay        crop vertical du gameplay (centre ou suivi du mouvement)
  streamer        webcam dominante (60 %) + gameplay
  split           webcam en haut / gameplay en bas
  webcam_overlay  gameplay plein écran + webcam en incrustation
  tracking        crop dynamique qui suit le visage (ou le mouvement)
  blur_fit        image entière + fond flouté (jamais de crop catastrophique)
  auto            choisit selon la détection de visage
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.clips.tracking import Box, FaceTrack, build_face_detector, smooth_path, track_faces
from app.core.config import Settings, get_settings
from app.video.analysis import AnalysisResult

OUT_W, OUT_H = 1080, 1920
LAYOUTS = ("gameplay", "streamer", "split", "webcam_overlay", "tracking", "blur_fit")


@dataclass
class CompositionPlan:
    layout: str
    filter: str                     # filtergraph : entrée [0:v], sortie [vcomp]
    subtitle_y: float               # position verticale conseillée pour les sous-titres (0-1)
    hook_y: float = 0.12
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"layout": self.layout, "subtitle_y": self.subtitle_y, "hook_y": self.hook_y, **self.details}


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def piecewise_expr(keyframes: list[tuple[float, float]]) -> str:
    """Expression FFmpeg linéaire par morceaux de t (évaluée à chaque image par le filtre crop)."""
    kf = [(round(t, 2), round(v, 1)) for t, v in keyframes]
    if len(kf) == 1 or all(abs(v - kf[0][1]) < 1 for _, v in kf):
        return str(int(kf[0][1]))
    expr = str(kf[-1][1])
    for (t0, v0), (t1, v1) in reversed(list(zip(kf, kf[1:]))):
        dt = max(0.01, t1 - t0)
        expr = f"if(lt(t,{t1}),{v0}+({v1}-{v0})*(t-{t0})/{dt},{expr})"
    return expr


class VideoCompositionService:
    def __init__(self, settings: Settings | None = None, face_detector=None):
        self.s = settings or get_settings()
        self.detector = face_detector or build_face_detector(self.s.get("composition.face_detector", "haar"))

    # ------------------------------------------------------------------ décision
    def plan(self, video: Path, src_w: int, src_h: int, start: float, end: float,
             analysis: AnalysisResult | None = None, layout: str | None = None,
             crop_override: dict | None = None) -> CompositionPlan:
        layout = (layout or self.s.get("composition.layout", "auto")).lower()
        crop_override = crop_override or {}
        if crop_override.get("layout"):
            layout = crop_override["layout"]
        webcam_cfg = crop_override.get("webcam_region") or self.s.get("composition.webcam_region")
        webcam = Box(*webcam_cfg) if webcam_cfg else None

        # Source déjà verticale : pas de crop, simple mise à l'échelle.
        if src_w / src_h <= OUT_W / OUT_H + 0.02:
            return self._fit_vertical(src_w, src_h)

        track: FaceTrack | None = None
        if self.detector is not None and layout in ("auto", "split", "streamer", "tracking", "webcam_overlay"):
            track = track_faces(self.detector, video, start, end, samples=10)

        if layout == "auto":
            layout, webcam = self._auto_layout(track, webcam)
        if layout in ("split", "streamer", "webcam_overlay") and webcam is None:
            webcam = self._webcam_from_track(track)
            if webcam is None:  # pas de webcam détectée -> gameplay
                layout = "gameplay"

        details = {"face_hit_ratio": round(track.hit_ratio, 2) if track else None}
        manual_x = crop_override.get("x")  # centre horizontal manuel (0-1)
        if layout == "blur_fit":
            plan = self._blur_fit(src_w, src_h)
        elif layout == "tracking":
            plan = self._tracking(src_w, src_h, start, end, track, analysis, manual_x)
        elif layout in ("split", "streamer"):
            plan = self._split(src_w, src_h, start, end, webcam, analysis, manual_x,
                               top_ratio=0.6 if layout == "streamer" else float(self.s.get("composition.split_top_ratio", 0.36)),
                               name=layout, track=track)
        elif layout == "webcam_overlay":
            plan = self._overlay(src_w, src_h, start, end, webcam, analysis, manual_x)
        else:
            plan = self._gameplay(src_w, src_h, start, end, analysis, manual_x)
        plan.details = {**details, **plan.details}
        return plan

    def _auto_layout(self, track: FaceTrack | None, webcam: Box | None) -> tuple[str, Box | None]:
        if webcam is not None:
            return "split", webcam
        if track and track.hit_ratio >= 0.4:
            med = track.median_box()
            if med and med.area < 0.04 and track.center_std() < 0.12:
                return "split", self._webcam_from_track(track)   # facecam incrustée dans un stream
            if med and med.area >= 0.04:
                return "tracking", None                           # streamer plein cadre / IRL
        return "gameplay", None

    @staticmethod
    def _webcam_from_track(track: FaceTrack | None) -> Box | None:
        if not track or track.hit_ratio < 0.3:
            return None
        m = track.median_box()
        if m is None:
            return None
        # zone webcam estimée : visage élargi (épaules + marge)
        w, h = min(1.0, m.w * 3.0), min(1.0, m.h * 3.0)
        x = float(np.clip(m.cx - w / 2, 0, 1 - w))
        y = float(np.clip(m.cy - h * 0.45, 0, 1 - h))
        return Box(x, y, w, h)

    # ------------------------------------------------------------------ chemins de crop
    def _x_path(self, src_w: int, crop_w: int, start: float, end: float, analysis: AnalysisResult | None,
                centers: list[tuple[float, float]] | None = None, manual_x: float | None = None) -> tuple[str, list]:
        """Retourne l'expression x(t) du crop et les keyframes (t relatif au clip)."""
        max_x = src_w - crop_w
        if manual_x is not None:
            x = float(np.clip(manual_x * src_w - crop_w / 2, 0, max_x))
            return str(int(x)), [(0.0, x)]
        mode = self.s.get("composition.gameplay_mode", "saliency")
        if centers is None and analysis is not None and mode == "saliency" and analysis.motion_center_x:
            step = analysis.step
            i0, i1 = analysis.index(start), analysis.index(end)
            raw = analysis.motion_center_x[i0:i1 + 1]
            motion = analysis.motion[i0:i1 + 1]
            centers = []
            per_sec = max(1, int(1 / step))
            for k in range(0, len(raw), per_sec):
                chunk = [(c, m) for c, m in zip(raw[k:k + per_sec * 2], motion[k:k + per_sec * 2])]
                wsum = sum(m for _, m in chunk)
                c = sum(c * m for c, m in chunk) / wsum if wsum > 0 else 0.5
                # le centre de l'écran reste prioritaire (HUD, viseur) : attraction vers 0,5
                centers.append((k * step, 0.6 * c + 0.4 * 0.5))
        if not centers:
            x = max_x / 2
            return str(int(x)), [(0.0, x)]
        alpha = float(self.s.get("composition.tracking_smoothing", 0.85))
        times = [t for t, _ in centers]
        vals = smooth_path(times, [c for _, c in centers], alpha=alpha, dead_zone=0.06)
        kf = [(t, float(np.clip(v * src_w - crop_w / 2, 0, max_x))) for t, v in zip(times, vals)]
        return piecewise_expr(kf), kf

    # ------------------------------------------------------------------ layouts
    def _fit_vertical(self, src_w: int, src_h: int) -> CompositionPlan:
        f = (f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
             f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[vcomp]")
        return CompositionPlan("fit_vertical", f, subtitle_y=0.72)

    def _blur_fit(self, src_w: int, src_h: int) -> CompositionPlan:
        fg_h = _even(OUT_W * src_h / src_w)
        f = (f"[0:v]split=2[bgsrc][fgsrc];"
             f"[bgsrc]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
             f"boxblur=luma_radius=30:luma_power=2,eq=brightness=-0.08[bg];"
             f"[fgsrc]scale={OUT_W}:{fg_h}[fg];"
             f"[bg][fg]overlay=0:(H-h)/2,setsar=1[vcomp]")
        bottom_of_fg = (OUT_H + fg_h) / 2 / OUT_H
        return CompositionPlan("blur_fit", f, subtitle_y=min(0.8, bottom_of_fg + 0.08),
                               hook_y=max(0.08, (OUT_H - fg_h) / 2 / OUT_H - 0.08),
                               details={"crop_coverage": 1.0})

    def _gameplay(self, src_w, src_h, start, end, analysis, manual_x) -> CompositionPlan:
        crop_w = _even(min(src_w, src_h * OUT_W / OUT_H))
        x_expr, kf = self._x_path(src_w, crop_w, start, end, analysis, manual_x=manual_x)
        f = f"[0:v]crop={crop_w}:{src_h}:'{x_expr}':0,scale={OUT_W}:{OUT_H},setsar=1[vcomp]"
        return CompositionPlan("gameplay", f, subtitle_y=0.70,
                               details={"crop_w": crop_w, "keyframes": len(kf), "crop_coverage": round(crop_w / src_w, 3)})

    def _tracking(self, src_w, src_h, start, end, track, analysis, manual_x) -> CompositionPlan:
        crop_w = _even(min(src_w, src_h * OUT_W / OUT_H))
        centers = None
        face_inside = None
        if track and track.hit_ratio >= 0.3:
            pts = [(t, b.cx) for t, b in track.samples if b]
            centers = pts if pts[0][0] < 0.5 else [(0.0, pts[0][1])] + pts
        x_expr, kf = self._x_path(src_w, crop_w, start, end, analysis, centers=centers, manual_x=manual_x)
        if track and kf:
            inside = 0
            boxes = [(t, b) for t, b in track.samples if b]
            for t, b in boxes:
                x = float(np.interp(t, [k[0] for k in kf], [k[1] for k in kf])) / src_w
                inside += int(b.x >= x - 0.005 and b.x + b.w <= x + crop_w / src_w + 0.005)
            face_inside = round(inside / max(1, len(boxes)), 2)
        f = f"[0:v]crop={crop_w}:{src_h}:'{x_expr}':0,scale={OUT_W}:{OUT_H},setsar=1[vcomp]"
        return CompositionPlan("tracking", f, subtitle_y=0.74,
                               details={"crop_w": crop_w, "keyframes": len(kf), "face_inside_ratio": face_inside,
                                        "crop_coverage": round(crop_w / src_w, 3), "tracked": "face" if centers else "motion"})

    def _webcam_crop(self, src_w, src_h, cam: Box, target_w: int, target_h: int) -> tuple[int, int, int, int]:
        """Crop dans la zone webcam au ratio de la cible, centré sur la zone."""
        cx, cy = cam.cx * src_w, cam.cy * src_h
        ratio = target_w / target_h
        cw, ch = cam.w * src_w, cam.h * src_h
        if cw / ch > ratio:
            cw = ch * ratio
        else:
            ch = cw / ratio
        cw, ch = _even(cw), _even(ch)
        x = int(np.clip(cx - cw / 2, 0, src_w - cw))
        y = int(np.clip(cy - ch / 2, 0, src_h - ch))
        return cw, ch, x, y

    def _split(self, src_w, src_h, start, end, cam: Box, analysis, manual_x, top_ratio: float, name: str,
               track: FaceTrack | None) -> CompositionPlan:
        top_h = _even(OUT_H * top_ratio)
        bot_h = OUT_H - top_h
        cw, ch, cx, cy = self._webcam_crop(src_w, src_h, cam, OUT_W, top_h)
        # gameplay : crop au ratio de la zone basse
        g_w = _even(min(src_w, src_h * OUT_W / bot_h))
        g_h = _even(min(src_h, g_w * bot_h / OUT_W))
        x_expr, kf = self._x_path(src_w, g_w, start, end, analysis, manual_x=manual_x)
        g_y = int((src_h - g_h) / 2)
        f = (f"[0:v]split=2[camsrc][gsrc];"
             f"[camsrc]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{top_h},setsar=1[cam];"
             f"[gsrc]crop={g_w}:{g_h}:'{x_expr}':{g_y},scale={OUT_W}:{bot_h},setsar=1[game];"
             f"[cam][game]vstack=inputs=2[vcomp]")
        face_inside = None
        if track:
            boxes = track.boxes()
            if boxes:
                fx0, fy0, fx1, fy1 = cx / src_w, cy / src_h, (cx + cw) / src_w, (cy + ch) / src_h
                face_inside = round(sum(1 for b in boxes if b.x >= fx0 and b.y >= fy0 and b.x + b.w <= fx1
                                        and b.y + b.h <= fy1) / len(boxes), 2)
        return CompositionPlan(name, f, subtitle_y=top_h / OUT_H + 0.02, hook_y=0.06,
                               details={"webcam_region": cam.to_list(), "split_top_px": top_h,
                                        "face_inside_ratio": face_inside, "keyframes": len(kf),
                                        "crop_coverage": round(g_w / src_w, 3)})

    def _overlay(self, src_w, src_h, start, end, cam: Box, analysis, manual_x) -> CompositionPlan:
        crop_w = _even(min(src_w, src_h * OUT_W / OUT_H))
        x_expr, kf = self._x_path(src_w, crop_w, start, end, analysis, manual_x=manual_x)
        ow = 420
        oh = _even(ow * cam.h * src_h / max(1e-6, cam.w * src_w))
        cw, ch, cx, cy = self._webcam_crop(src_w, src_h, cam, ow, oh)
        f = (f"[0:v]split=2[camsrc][gsrc];"
             f"[gsrc]crop={crop_w}:{src_h}:'{x_expr}':0,scale={OUT_W}:{OUT_H},setsar=1[game];"
             f"[camsrc]crop={cw}:{ch}:{cx}:{cy},scale={ow}:{oh}[cam];"
             f"[game][cam]overlay=W-w-40:260[vcomp]")
        return CompositionPlan("webcam_overlay", f, subtitle_y=0.70,
                               details={"webcam_region": cam.to_list(), "keyframes": len(kf),
                                        "crop_coverage": round(crop_w / src_w, 3)})
