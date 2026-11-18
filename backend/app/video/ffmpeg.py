"""Petits wrappers FFmpeg/FFprobe (toute la lourde manipulation vidéo passe par FFmpeg)."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.core.utils import get_logger

log = get_logger("ffmpeg")


class FFmpegError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise FFmpegError("FFmpeg/FFprobe introuvables dans le PATH. Voir README > Installation FFmpeg.")


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"Commande échouée ({cmd[0]}): {tail}")
    return proc


@dataclass
class ProbeResult:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_codec: str | None
    audio_codec: str | None
    pix_fmt: str | None = None
    audio_sample_rate: int | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _parse_rate(rate: str | None) -> float:
    if not rate or rate == "0/0":
        return 0.0
    if "/" in rate:
        n, d = rate.split("/")
        return float(n) / float(d) if float(d) else 0.0
    return float(rate)


def probe(path: Path | str) -> ProbeResult:
    require_ffmpeg()
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]).stdout
    data = json.loads(out)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if v is None:
        raise FFmpegError(f"Aucun flux vidéo dans {path}")
    duration = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    return ProbeResult(
        duration=duration,
        width=int(v.get("width", 0)),
        height=int(v.get("height", 0)),
        fps=round(_parse_rate(v.get("avg_frame_rate")) or _parse_rate(v.get("r_frame_rate")), 3),
        has_audio=a is not None,
        video_codec=v.get("codec_name"),
        audio_codec=a.get("codec_name") if a else None,
        pix_fmt=v.get("pix_fmt"),
        audio_sample_rate=int(a["sample_rate"]) if a and a.get("sample_rate") else None,
    )


def extract_audio(src: Path, dst: Path, sample_rate: int = 16000) -> Path:
    """Audio mono 16 kHz PCM : format natif de Whisper et facile à lire avec numpy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sample_rate),
         "-c:a", "pcm_s16le", str(dst)])
    return dst


def read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    import wave

    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        width = w.getsampwidth()
        channels = w.getnchannels()
    if width != 2:
        raise FFmpegError("WAV attendu en 16 bits")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sr


def iter_gray_frames(src: Path, fps: float, width: int, start: float | None = None, duration: float | None = None):
    """Itère des images en niveaux de gris basse résolution (décodage FFmpeg, très rapide)."""
    info = probe(src)
    height = int(round(info.height * width / max(1, info.width) / 2) * 2) or width
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vf", f"fps={fps},scale={width}:{height}:flags=area,format=gray", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_size = width * height
    idx = 0
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            t = (start or 0.0) + idx / fps
            yield t, np.frombuffer(buf, dtype=np.uint8).reshape(height, width)
            idx += 1
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()


def extract_frame(src: Path, t: float, dst: Path, width: int | None = None) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = ["-vf", f"scale={width}:-2"] if width else []
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0, t):.3f}", "-i", str(src), "-frames:v", "1", *vf,
         "-q:v", "3", str(dst)])
    return dst


def read_frame_bgr(src: Path, t: float, width: int | None = None) -> np.ndarray | None:
    """Une image couleur (BGR) à l'instant t, pour la détection de visage."""
    info = probe(src)
    w = width or info.width
    h = int(round(info.height * w / max(1, info.width) / 2) * 2)
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.3f}", "-i", str(src), "-frames:v", "1",
         "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or len(proc.stdout) < w * h * 3:
        return None
    return np.frombuffer(proc.stdout[: w * h * 3], dtype=np.uint8).reshape(h, w, 3)


def has_encoder(name: str) -> bool:
    try:
        return name in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except Exception:
        return False
