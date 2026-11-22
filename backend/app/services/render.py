"""ExportService (rendu) : un appel FFmpeg = découpe + cadrage + sous-titres + hook + audio normalisé."""
from __future__ import annotations

import time
from pathlib import Path

from app.clips.composition import CompositionPlan
from app.core.config import Settings, get_settings
from app.core.hardware import pick_video_encoder
from app.core.utils import get_logger
from app.video import ffmpeg

log = get_logger("render")

MAX_INLINE_GRAPH = 100_000   # caractères (limite d'un argument de commande sous Linux : 128 Kio)


def _escape_filter_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def effects_chain(peak: float | None, duration: float, cfg: dict, progress_bar: bool = True) -> tuple[str, str]:
    """Effets « punchy » (expressions FFmpeg évaluées image par image, aucun ré-encodage en plus) :
    - zoom punch-in sur le moment fort + léger tremblement + flash blanc ;
    - barre de progression en haut (les spectateurs voient que c'est court : ils restent jusqu'au bout).
    Renvoie (filtres avant les sous-titres, filtres après) ; chaînes vides si rien à faire."""
    if not cfg or not cfg.get("enabled", True):
        return "", ""
    pre: list[str] = []
    if peak is not None and 0 <= peak < duration - 0.3:
        P = f"{peak:.3f}"
        d = float(cfg.get("punch_duration", 0.9))
        z = float(cfg.get("punch_zoom", 1.15))
        if z > 1.001:
            Z = f"if(between(t,{P},{P}+{d}),1+{z - 1:.3f}*min(1,(t-{P})/0.12)*min(1,({P}+{d}-t)/0.25),1)"
            dx = dy = "0"
            if cfg.get("shake", True):
                amp = int(cfg.get("shake_px", 12))
                env = f"between(t,{P},{P}+0.35)*(1-(t-{P})/0.35)"
                dx = f"{amp}*sin(2*PI*t*18)*{env}"
                dy = f"{amp}*cos(2*PI*t*23)*{env}"
            pre.append(f"scale=w='2*trunc(1080*({Z})/2)':h='2*trunc(1920*({Z})/2)':eval=frame")
            pre.append(f"crop=1080:1920:x='clip((iw-1080)/2+{dx},0,iw-1080)':y='clip((ih-1920)/2+{dy},0,ih-1920)'")
        if cfg.get("flash", True):
            pre.append(f"eq=brightness='if(between(t,{P},{P}+0.1),0.22*(1-(t-{P})/0.1),0)':eval=frame")
    post = ""
    if progress_bar and cfg.get("progress_bar", True) and duration > 1:
        h = int(cfg.get("bar_height", 10))
        color = str(cfg.get("bar_color", "0xFFD400"))
        post = (f"color=c={color}:s=1080x{h}:r=30,format=yuva420p[pbar];"
                f"{{IN}}[pbar]overlay=x='-w+w*min(1,t/{duration:.3f})':y=0:shortest=1")
    return ",".join(pre), post


class RenderService:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def output_fps(self, src_fps: float) -> int:
        cfg = self.s.get("output.fps", "auto")
        if str(cfg) != "auto":
            return int(cfg)
        return 60 if src_fps >= 50 else 30

    def render(self, src: Path, start: float, end: float, plan: CompositionPlan, out_path: Path,
               ass_path: Path | None, has_audio: bool, src_fps: float,
               keep: list[tuple[float, float]] | None = None, peak: float | None = None,
               progress_bar: bool = True) -> float:
        """`keep` : plages (relatives au début du clip) à conserver ; le reste (blancs) est coupé.
        `peak` : instant du moment fort dans le clip final (après coupes), pour les effets."""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fps = self.output_fps(src_fps)
        graph = plan.filter
        tail = "[vcomp]"
        cut = ""
        if keep:
            expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)
            graph += f";[vcomp]select='{expr}',setpts=N/FRAME_RATE/TB[vcut]"
            tail = "[vcut]"
            cut = f"aselect='{expr}',asetpts=N/SR/TB,"
        out_duration = sum(b - a for a, b in keep) if keep else max(0.1, end - start)
        pre, post = effects_chain(peak, out_duration, dict(self.s.get("effects", {}) or {}), progress_bar)
        if pre:
            graph += f";{tail}{pre}[vfx]"
            tail = "[vfx]"
        if ass_path is not None:
            graph += f";{tail}ass=filename='{_escape_filter_path(ass_path)}'[vsub]"
            tail = "[vsub]"
        if post:
            graph += ";" + post.replace("{IN}", tail) + "[vbar]"
            tail = "[vbar]"
        graph += f";{tail}fps={fps},format=yuv420p[vout]"
        if has_audio:
            af = "aresample=48000"
            if self.s.get("output.loudnorm", True):
                af = "loudnorm=I=-14:TP=-1.5:LRA=11," + af
            graph += f";[0:a]{cut}{af}[aout]"
        # Graphe passé directement dans la commande : `-filter_complex` (forme non dépréciée, toutes versions).
        # L'ancienne option `-filter_complex_script` a été supprimée dans FFmpeg 9. Pour un graphe énorme
        # (au-delà de la limite d'un argument sous Linux), on utilise `-/filter_complex fichier` (FFmpeg ≥ 7.1).
        script: Path | None = None
        if len(graph) <= MAX_INLINE_GRAPH:
            filter_args = ["-filter_complex", graph]
        else:
            script = out_path.with_suffix(".filter.txt")
            script.write_text(graph)
            filter_args = ["-/filter_complex", str(script)]

        duration = max(0.1, end - start)
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
        if not has_audio:
            cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        cmd += [*filter_args, "-map", "[vout]"]
        cmd += ["-map", "[aout]"] if has_audio else ["-map", "1:a"]
        encoder = pick_video_encoder(self.s.get("output.video_codec", "auto"))
        if encoder == "h264_nvenc":
            cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(int(self.s.get("output.crf", 21)) + 2)]
        else:
            cmd += ["-c:v", "libx264", "-preset", str(self.s.get("output.preset", "veryfast")),
                    "-crf", str(self.s.get("output.crf", 21)), "-profile:v", "high"]
        cmd += ["-r", str(fps), "-c:a", "aac", "-b:a", str(self.s.get("output.audio_bitrate", "160k")),
                "-ar", "48000", "-ac", "2", "-movflags", "+faststart", "-shortest", str(out_path)]
        t0 = time.time()
        ffmpeg.run(cmd)
        if script is not None:
            script.unlink(missing_ok=True)
        elapsed = time.time() - t0
        log.info("Rendu %s (%.1fs de clip) en %.1fs", out_path.name, duration, elapsed)
        return elapsed
