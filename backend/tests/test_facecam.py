"""Facecam : visage stable (pas les personnages du jeu), cadre réel de la webcam, split automatique."""
import subprocess

import numpy as np
import pytest

from app.clips.composition import VideoCompositionService
from app.clips.tracking import YUNET_MODEL, Box, build_face_detector, detect_webcam_box, track_faces

from .conftest import requires_ffmpeg

pytestmark = requires_ffmpeg
CAM = (40, 400, 300, 240)   # x, y, w, h en pixels sur 1280x720


@pytest.fixture(scope="module")
def stream_video(tmp_path_factory):
    """8 s de « stream » : jeu qui bouge partout + webcam fixe encadrée en bas à gauche."""
    import cv2

    out = tmp_path_factory.mktemp("stream") / "stream.mp4"
    W, H, fps = 1280, 720, 15
    p = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                          "-r", str(fps), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
                         stdin=subprocess.PIPE)
    rng = np.random.default_rng(0)
    x, y, w, h = CAM
    for i in range(fps * 8):
        fr = np.zeros((H, W, 3), np.uint8)
        for k in range(0, W, 80):
            cv2.rectangle(fr, ((k + i * 9) % W, 0), ((k + i * 9) % W + 40, H), (30, 90 + k % 120, 60), -1)
        for _ in range(25):
            cv2.circle(fr, (int(rng.integers(0, W)), int(rng.integers(0, H))), int(rng.integers(10, 60)),
                       tuple(int(c) for c in rng.integers(0, 255, 3)), -1)
        fr[y:y + h, x:x + w] = (120, 140, 170)
        cv2.ellipse(fr, (x + w // 2 + (i % 6) - 3, y + h // 2), (50, 65), 0, 0, 360, (90, 110, 200), -1)
        cv2.rectangle(fr, (x - 3, y - 3), (x + w + 2, y + h + 2), (255, 255, 255), 3)
        p.stdin.write(fr.tobytes())
    p.stdin.close()
    p.wait()
    return out


FACE = Box(0.13, 0.60, 0.07, 0.17)   # visage dans la webcam


class NpcAndStreamer:
    """Le streamer (fixe) + un personnage du jeu plus grand qui traverse l'écran."""

    def __init__(self):
        self.n = 0

    def detect(self, frame):
        self.n += 1
        npc = Box(0.1 + 0.08 * self.n, 0.2, 0.12, 0.2)
        return [npc, FACE]


def test_track_follows_stable_face_not_game_characters(stream_video):
    track = track_faces(NpcAndStreamer(), stream_video, 0, 8)
    assert track.hit_ratio == 1.0
    assert all(b == FACE for b in track.boxes())


def test_webcam_frame_detected_from_still_region(stream_video):
    box = detect_webcam_box(stream_video, 0, 8, FACE)
    assert box is not None
    ex, ey, ew, eh = CAM[0] / 1280, CAM[1] / 720, CAM[2] / 1280, CAM[3] / 720
    assert abs(box.x - ex) < 0.02 and abs(box.y - ey) < 0.03
    assert abs(box.w - ew) < 0.03 and abs(box.h - eh) < 0.04


def test_no_webcam_frame_when_face_is_in_moving_area(stream_video):
    assert detect_webcam_box(stream_video, 0, 8, Box(0.6, 0.2, 0.05, 0.09)) is None


def test_auto_split_uses_detected_webcam(stream_video, settings):
    class Fixed:
        def detect(self, frame):
            return [FACE]

    plan = VideoCompositionService(settings, face_detector=Fixed()).plan(stream_video, 1280, 720, 0, 8)
    assert plan.layout == "split" and plan.details["webcam_source"] == "detected"


@pytest.mark.skipif(not YUNET_MODEL.exists(), reason="modèle YuNet absent")
def test_yunet_is_default_detector():
    det = build_face_detector("yunet")
    assert type(det).__name__ == "YuNetFaceDetector"
    assert det.detect(np.zeros((360, 640, 3), np.uint8)) == []
