"""QualityControlService : vérifications automatiques avant export.

Échec d'une vérification bloquante -> status « needs_review » + raisons lisibles.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.core.config import Settings, get_settings
from app.subtitles.ass import PLAY_W, SAFE_MARGIN_X, SubEvent
from app.video import ffmpeg


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


@dataclass
class QCReport:
    passed: bool
    checks: list[Check] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def reasons(self) -> list[str]:
        return [c.detail for c in self.checks if not c.ok and c.blocking]

    @property
    def warnings(self) -> list[str]:
        return [c.detail for c in self.checks if not c.ok and not c.blocking]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reasons": self.reasons, "warnings": self.warnings,
                "checks": [asdict(c) for c in self.checks], "metrics": self.metrics}


def scan_media(path: Path) -> dict:
    """Une seule passe de décodage : erreurs, images noires, volume."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-v", "info", "-i", str(path),
         "-vf", "blackdetect=d=0.3:pix_th=0.10", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    err = proc.stderr
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]
    mean_vol = re.search(r"mean_volume: (-?[\d.]+) dB", err)
    decode_errors = [ln for ln in err.splitlines() if re.search(r"(error|corrupt|invalid)", ln, re.I)
                     and "Error while" not in ln and "volumedetect" not in ln]
    return {
        "returncode": proc.returncode,
        "black_segments": blacks,
        "mean_volume": float(mean_vol.group(1)) if mean_vol else None,
        "decode_errors": decode_errors[:5],
    }


class QualityControlService:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def check(self, clip_path: Path, events: list[SubEvent] | None = None, plan: dict | None = None,
              expect_subtitles: bool = True, hook_text: str | None = None) -> QCReport:
        checks: list[Check] = []
        plan = plan or {}
        min_d = float(self.s.get("video.min_clip_duration", 15))
        max_d = float(self.s.get("video.max_clip_duration", 60))
        w, h = [int(x) for x in str(self.s.get("output.resolution", "1080x1920")).split("x")]

        try:
            info = ffmpeg.probe(clip_path)
        except Exception as exc:
            return QCReport(False, [Check("file_valid", False, f"Fichier vidéo illisible : {exc}")])
        scan = scan_media(clip_path)
        checks.append(Check("file_valid", scan["returncode"] == 0 and not scan["decode_errors"],
                            "Fichier valide" if scan["returncode"] == 0 and not scan["decode_errors"]
                            else f"Erreurs de décodage : {scan['decode_errors'][:2]}"))
        checks.append(Check("duration", min_d - 0.6 <= info.duration <= max_d + 0.6,
                            f"Durée {info.duration:.1f}s (attendu {min_d:.0f}-{max_d:.0f}s)"))
        checks.append(Check("resolution", (info.width, info.height) == (w, h),
                            f"Résolution {info.width}x{info.height} (attendu {w}x{h})"))
        checks.append(Check("codecs", info.video_codec == "h264" and info.audio_codec == "aac",
                            f"Codecs {info.video_codec}/{info.audio_codec} (attendu h264/aac)"))
        silent = scan["mean_volume"] is None or scan["mean_volume"] < -55
        checks.append(Check("audio", info.has_audio and not silent,
                            "Audio présent" if info.has_audio and not silent
                            else f"Audio absent ou silencieux (volume moyen {scan['mean_volume']} dB)"))
        black_total = sum(b - a for a, b in scan["black_segments"])
        black_start = any(a < 0.2 and b - a > 0.3 for a, b in scan["black_segments"])
        checks.append(Check("black_frames", black_total <= 1.0 and not black_start,
                            "Pas d'image noire" if black_total <= 1.0 and not black_start
                            else f"Images noires : {black_total:.1f}s" + (" dès le début" if black_start else "")))

        # cadrage
        face_ratio = plan.get("face_inside_ratio")
        if face_ratio is not None:
            checks.append(Check("face_framing", face_ratio >= 0.8,
                                f"Visage dans le cadre sur {face_ratio:.0%} des images" + ("" if face_ratio >= 0.8 else " — visage coupé")))
        cov = plan.get("crop_coverage")
        if cov is not None:
            checks.append(Check("crop", cov >= 0.25,
                                f"Couverture du crop {cov:.0%} de la largeur source", blocking=cov < 0.2))

        # sous-titres
        if expect_subtitles:
            events = events or []
            if not events:
                checks.append(Check("subtitles_present", False, "Aucun sous-titre généré alors que de la parole est attendue",
                                    blocking=False))
            else:
                in_range = all(0 <= e.start < e.end <= info.duration + 0.1 for e in events)
                monotonic = all(a.start <= b.start for a, b in zip(events, events[1:]))
                checks.append(Check("subtitles_sync", in_range and monotonic,
                                    "Sous-titres synchronisés" if in_range and monotonic
                                    else "Sous-titres hors de la durée du clip ou désordonnés"))
                safe_w = PLAY_W - 2 * SAFE_MARGIN_X
                too_wide = [e for e in events if e.width_est > safe_w * 1.02]
                checks.append(Check("text_fits", not too_wide,
                                    "Texte dans la zone sûre" if not too_wide
                                    else f"{len(too_wide)} sous-titre(s) dépassent de l'écran"))
        if hook_text:
            checks.append(Check("hook_length", len(hook_text) <= 48, f"Hook de {len(hook_text)} caractères",
                                blocking=False))

        passed = all(c.ok for c in checks if c.blocking)
        return QCReport(passed, checks, {"duration": info.duration, "black_seconds": round(black_total, 2),
                                         "mean_volume": scan["mean_volume"], "fps": info.fps})
