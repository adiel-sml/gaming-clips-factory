"""VideoAnalysisService : signaux bas niveau sur une timeline régulière.

Produit, à pas fixe (0,5 s par défaut) :
  - audio_db       : niveau sonore (dBFS, RMS)
  - motion         : intensité de mouvement (différence moyenne entre images)
  - scene_cut      : 1 si changement de plan
  - brightness     : luminosité moyenne (détection d'images noires)
  - dhash          : hash perceptuel 64 bits (1 par seconde) pour la déduplication
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.core.config import Settings, get_settings
from app.core.utils import get_logger, read_json, write_json
from app.video import ffmpeg

log = get_logger("analysis")


def dhash(gray: np.ndarray) -> int:
    """Difference hash 64 bits sur une image en niveaux de gris."""
    import cv2

    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int(sum(1 << i for i, b in enumerate(bits) if b))


def hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


@dataclass
class AnalysisResult:
    step: float
    duration: float
    audio_db: list[float]
    motion: list[float]
    scene_cut: list[int]
    brightness: list[float]
    dhash: list[str] = field(default_factory=list)   # par seconde, hex
    motion_center_x: list[float] = field(default_factory=list)  # centre du mouvement (0-1) par pas
    probe: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "AnalysisResult":
        return cls(**d)

    def arr(self, name: str) -> np.ndarray:
        return np.asarray(getattr(self, name), dtype=np.float32)

    def index(self, t: float) -> int:
        return int(np.clip(round(t / self.step), 0, max(0, len(self.audio_db) - 1)))


class VideoAnalysisService:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def analyze(self, video_path: Path, work_dir: Path, force: bool = False) -> AnalysisResult:
        cache = work_dir / "analysis.json"
        if not force and (cached := read_json(cache)):
            return AnalysisResult.from_dict(cached)
        info = ffmpeg.probe(video_path)
        step = float(self.s.get("analysis.step", 0.5))
        n = max(1, int(np.ceil(info.duration / step)))

        audio_db = np.full(n, -90.0, dtype=np.float32)
        if info.has_audio:
            wav = self.audio_path(video_path, work_dir)
            samples, sr = ffmpeg.read_wav_mono(wav)
            audio_db = self._audio_levels(samples, sr, step, n)

        motion, cuts, bright, hashes, centers = self._visual(video_path, step, n)
        result = AnalysisResult(
            step=step, duration=info.duration,
            audio_db=[round(float(x), 2) for x in audio_db],
            motion=[round(float(x), 3) for x in motion],
            scene_cut=[int(x) for x in cuts],
            brightness=[round(float(x), 1) for x in bright],
            dhash=hashes,
            motion_center_x=[round(float(x), 3) for x in centers],
            probe=info.to_dict(),
        )
        write_json(cache, result.to_dict())
        return result

    # ------------------------------------------------------------------
    def audio_path(self, video_path: Path, work_dir: Path) -> Path:
        wav = work_dir / "audio_16k.wav"
        if not wav.exists():
            ffmpeg.extract_audio(video_path, wav)
        return wav

    @staticmethod
    def _audio_levels(samples: np.ndarray, sr: int, step: float, n: int) -> np.ndarray:
        hop = int(sr * step)
        out = np.full(n, -90.0, dtype=np.float32)
        for i in range(n):
            chunk = samples[i * hop:(i + 1) * hop]
            if chunk.size:
                rms = float(np.sqrt(np.mean(chunk ** 2)) + 1e-9)
                out[i] = max(-90.0, 20 * np.log10(rms))
        return out

    def _visual(self, video_path: Path, step: float, n: int):
        fps = float(self.s.get("analysis.frame_sample_fps", 4))
        width = int(self.s.get("analysis.frame_width", 160))
        threshold = float(self.s.get("analysis.scene_cut_threshold", 28.0))
        motion = np.zeros(n, dtype=np.float32)
        counts = np.zeros(n, dtype=np.float32)
        cuts = np.zeros(n, dtype=np.int8)
        bright = np.zeros(n, dtype=np.float32)
        centers = np.full(n, 0.5, dtype=np.float32)
        hashes: list[str] = []
        prev = None
        next_hash_t = 0.0
        diffs_hist: list[float] = []
        for t, frame in ffmpeg.iter_gray_frames(video_path, fps, width):
            i = min(n - 1, int(t / step))
            bright[i] = max(bright[i], float(frame.mean()))
            if t >= next_hash_t:
                hashes.append(format(dhash(frame), "016x"))
                next_hash_t += 1.0
            if prev is not None:
                diff = np.abs(frame.astype(np.int16) - prev.astype(np.int16))
                d = float(diff.mean())
                motion[i] += d
                counts[i] += 1
                # changement de plan : saut fort par rapport à l'historique récent
                recent = np.median(diffs_hist[-int(fps * 2):]) if diffs_hist else 0.0
                if d > threshold and d > 3 * (recent + 1.0):
                    cuts[i] = 1
                diffs_hist.append(d)
                col = diff.sum(axis=0).astype(np.float64)
                if col.sum() > 0:
                    centers[i] = float((col * np.arange(col.size)).sum() / col.sum() / col.size)
            prev = frame
        motion = np.divide(motion, np.maximum(counts, 1))
        return motion, cuts, bright, hashes, centers
