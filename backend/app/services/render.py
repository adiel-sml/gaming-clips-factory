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


def _escape_filter_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


class RenderService:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def output_fps(self, src_fps: float) -> int:
        cfg = self.s.get("output.fps", "auto")
        if str(cfg) != "auto":
            return int(cfg)
        return 60 if src_fps >= 50 else 30

    def render(self, src: Path, start: float, end: float, plan: CompositionPlan, out_path: Path,
               ass_path: Path | None, has_audio: bool, src_fps: float) -> float:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fps = self.output_fps(src_fps)
        graph = plan.filter
        tail = "[vcomp]"
        if ass_path is not None:
            graph += f";{tail}ass=filename='{_escape_filter_path(ass_path)}'[vsub]"
            tail = "[vsub]"
        graph += f";{tail}fps={fps},format=yuv420p[vout]"
        if has_audio:
            af = "aresample=48000"
            if self.s.get("output.loudnorm", True):
                af = "loudnorm=I=-14:TP=-1.5:LRA=11," + af
            graph += f";[0:a]{af}[aout]"
        script = out_path.with_suffix(".filter.txt")
        script.write_text(graph)

        duration = max(0.1, end - start)
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
        if not has_audio:
            cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        cmd += ["-filter_complex_script", str(script), "-map", "[vout]"]
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
        script.unlink(missing_ok=True)
        elapsed = time.time() - t0
        log.info("Rendu %s (%.1fs de clip) en %.1fs", out_path.name, duration, elapsed)
        return elapsed
