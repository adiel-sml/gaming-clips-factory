"""Détection/suivi de visage. Interface `FaceDetector` remplaçable (MediaPipe, YOLO, YuNet… en phase 2+)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from app.video import ffmpeg


@dataclass
class Box:
    x: float  # normalisé 0-1
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return self.w * self.h

    def to_list(self) -> list[float]:
        return [round(self.x, 4), round(self.y, 4), round(self.w, 4), round(self.h, 4)]


class FaceDetector(Protocol):
    def detect(self, frame_bgr: np.ndarray) -> list[Box]: ...


class NoFaceDetector:
    def detect(self, frame_bgr: np.ndarray) -> list[Box]:
        return []


class HaarFaceDetector:
    """Cascade de Haar fournie avec OpenCV : rapide, sans téléchargement, CPU uniquement."""

    def __init__(self) -> None:
        import cv2

        path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        self._cascade = cv2.CascadeClassifier(str(path))
        self._cv2 = cv2

    def detect(self, frame_bgr: np.ndarray) -> list[Box]:
        cv2 = self._cv2
        h, w = frame_bgr.shape[:2]
        gray = cv2.equalizeHist(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY))
        faces = self._cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(max(24, w // 40),) * 2)
        boxes = [Box(x / w, y / h, fw / w, fh / h) for (x, y, fw, fh) in faces]
        return sorted(boxes, key=lambda b: b.area, reverse=True)


def build_face_detector(name: str) -> FaceDetector:
    if name == "haar":
        try:
            return HaarFaceDetector()
        except Exception:
            return NoFaceDetector()
    return NoFaceDetector()


@dataclass
class FaceTrack:
    samples: list[tuple[float, Box | None]]   # (temps relatif au début du clip, visage principal)

    @property
    def hit_ratio(self) -> float:
        return sum(1 for _, b in self.samples if b) / max(1, len(self.samples))

    def boxes(self) -> list[Box]:
        return [b for _, b in self.samples if b]

    def median_box(self) -> Box | None:
        bs = self.boxes()
        if not bs:
            return None
        arr = np.array([[b.x, b.y, b.w, b.h] for b in bs])
        m = np.median(arr, axis=0)
        return Box(*[float(v) for v in m])

    def center_std(self) -> float:
        bs = self.boxes()
        if len(bs) < 2:
            return 0.0
        return float(np.std([b.cx for b in bs]) + np.std([b.cy for b in bs]))


def track_faces(detector: FaceDetector, video: Path, start: float, end: float, samples: int = 10,
                width: int = 640) -> FaceTrack:
    duration = max(0.1, end - start)
    out: list[tuple[float, Box | None]] = []
    for k in range(samples):
        rel = duration * (k + 0.5) / samples
        frame = ffmpeg.read_frame_bgr(video, start + rel, width=width)
        boxes = detector.detect(frame) if frame is not None else []
        out.append((rel, boxes[0] if boxes else None))
    return FaceTrack(out)


def smooth_path(times: list[float], values: list[float], alpha: float, dead_zone: float) -> list[float]:
    """Lissage exponentiel + zone morte : la caméra ne bouge que si la cible s'éloigne vraiment."""
    if not values:
        return []
    out = [values[0]]
    current = values[0]
    for v in values[1:]:
        if abs(v - current) > dead_zone:
            current = alpha * current + (1 - alpha) * v
        out.append(current)
    return out
