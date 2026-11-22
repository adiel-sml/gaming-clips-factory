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


YUNET_MODEL = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"


class YuNetFaceDetector:
    """YuNet (OpenCV, réseau de neurones ~230 Ko, licence MIT) : fiable sur les petits visages
    (webcam incrustée dans un stream), éclairage difficile, tête légèrement de profil. CPU uniquement."""

    def __init__(self, score_threshold: float = 0.72, max_width: int = 960) -> None:
        import cv2

        if not YUNET_MODEL.exists():
            raise FileNotFoundError(YUNET_MODEL)
        self._cv2 = cv2
        self.max_width = max_width
        self._det = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (320, 320), score_threshold, 0.3, 50)

    def detect(self, frame_bgr: np.ndarray) -> list[Box]:
        h, w = frame_bgr.shape[:2]
        if w > self.max_width:
            scale = self.max_width / w
            frame_bgr = self._cv2.resize(frame_bgr, (self.max_width, int(h * scale)))
            h, w = frame_bgr.shape[:2]
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(frame_bgr)
        if faces is None:
            return []
        boxes = []
        for f in faces:
            x, y, fw, fh, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])
            if fw < 8 or fh < 8:
                continue
            boxes.append((score, Box(max(0.0, x / w), max(0.0, y / h), min(1.0, fw / w), min(1.0, fh / h))))
        return [b for _, b in sorted(boxes, key=lambda t: t[0], reverse=True)]


def build_face_detector(name: str) -> FaceDetector:
    """yunet (par défaut, recommandé) · haar (ancien) · none. Repli automatique si le modèle manque."""
    if name in ("yunet", "auto"):
        try:
            return YuNetFaceDetector()
        except Exception:
            name = "haar"
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


def _iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def track_faces(detector: FaceDetector, video: Path, start: float, end: float, samples: int = 10,
                width: int = 960) -> FaceTrack:
    """Suit LE visage du streamer : parmi tous les visages vus, celui qui revient le plus souvent au même
    endroit (une webcam ne bouge pas, contrairement aux personnages du jeu)."""
    duration = max(0.1, end - start)
    per_sample: list[tuple[float, list[Box]]] = []
    for k in range(samples):
        rel = duration * (k + 0.5) / samples
        frame = ffmpeg.read_frame_bgr(video, start + rel, width=width)
        per_sample.append((rel, detector.detect(frame) if frame is not None else []))
    candidates = [b for _, boxes in per_sample for b in boxes]
    if not candidates:
        return FaceTrack([(t, None) for t, _ in per_sample])

    def support(c: Box) -> tuple[int, float]:
        hits = [max((_iou(c, b) for b in boxes), default=0.0) for _, boxes in per_sample]
        return sum(1 for h in hits if h > 0.3), sum(hits)

    anchor = max(candidates, key=support)
    out: list[tuple[float, Box | None]] = []
    for t, boxes in per_sample:
        best = max(boxes, key=lambda b: _iou(anchor, b), default=None)
        out.append((t, best if best is not None and _iou(anchor, best) > 0.3 else None))
    # si aucun visage stable : le plus grand visage de chaque image (streamer plein cadre qui bouge)
    if sum(1 for _, b in out if b) < max(2, samples // 4):
        out = [(t, max(boxes, key=lambda b: b.area) if boxes else None) for t, boxes in per_sample]
    return FaceTrack(out)


def detect_webcam_box(video: Path, start: float, end: float, face: Box, samples: int = 8,
                      width: int = 480) -> Box | None:
    """Retrouve le cadre de la webcam autour du visage.

    Dans un stream, la pièce derrière le streamer reste quasi immobile alors que le jeu bouge sans cesse.
    On mesure la variation de chaque pixel sur plusieurs images : la zone calme, d'un seul tenant, qui
    contient le visage, c'est la webcam. Si le résultat n'est pas plausible (trop grand, forme bizarre),
    on renvoie None et l'appelant garde l'estimation autour du visage."""
    import cv2

    duration = max(0.1, end - start)
    frames = []
    for k in range(samples):
        frame = ffmpeg.read_frame_bgr(video, start + duration * (k + 0.5) / samples, width=width)
        if frame is not None:
            frames.append(cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.float32))
    if len(frames) < 4:
        return None
    std = np.std(np.stack(frames), axis=0)
    H, W = std.shape
    fx0, fy0 = int(face.x * W), int(face.y * H)
    fx1, fy1 = int(np.ceil((face.x + face.w) * W)), int(np.ceil((face.y + face.h) * H))
    static = (std < 10.0).astype(np.uint8)              # niveaux de gris 0-255 : < 10 = quasi immobile
    static[fy0:fy1, fx0:fx1] = 1                       # le visage bouge un peu : on l'inclut d'office
    static = cv2.morphologyEx(static, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))   # détache les zones calmes du jeu
    static[fy0:fy1, fx0:fx1] = 1
    static = cv2.morphologyEx(static, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(static, connectivity=4)
    lab = labels[min(H - 1, (fy0 + fy1) // 2), min(W - 1, (fx0 + fx1) // 2)]
    if lab == 0:
        return None
    x, y, bw, bh, area = (int(v) for v in stats[lab])
    fw, fh = max(1, fx1 - fx0), max(1, fy1 - fy0)
    plausible = (bw >= 1.5 * fw and bh >= 1.5 * fh and bw * bh <= 0.35 * W * H
                 and 0.5 < bw / bh < 2.6 and area >= 0.6 * bw * bh)   # rectangle bien rempli
    if not plausible:
        return None
    inset = 2                                           # retire le liseré éventuel autour de la webcam
    return Box((x + inset) / W, (y + inset) / H, (bw - 2 * inset) / W, (bh - 2 * inset) / H)


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
