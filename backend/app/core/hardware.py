"""Détection matérielle : CPU, GPU NVIDIA, VRAM, encodeurs FFmpeg.

Sert à adapter automatiquement le modèle Whisper et l'encodeur vidéo.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from functools import lru_cache


@dataclass
class HardwareInfo:
    cpu_count: int
    gpu_available: bool = False
    gpu_name: str | None = None
    vram_total_mb: int = 0
    vram_free_mb: int = 0
    cuda_for_whisper: bool = False
    nvenc_available: bool = False
    ffmpeg_version: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _nvidia_smi() -> tuple[str | None, int, int]:
    if not shutil.which("nvidia-smi"):
        return None, 0, 0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip().splitlines()
        if not out:
            return None, 0, 0
        name, total, free = [x.strip() for x in out[0].split(",")]
        return name, int(total), int(free)
    except Exception:
        return None, 0, 0


def _ffmpeg_info() -> tuple[str | None, bool]:
    if not shutil.which("ffmpeg"):
        return None, False
    try:
        version = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10).stdout.split("\n")[0]
        encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10).stdout
        return version, "h264_nvenc" in encoders
    except Exception:
        return None, False


@lru_cache(maxsize=1)
def detect_hardware() -> HardwareInfo:
    info = HardwareInfo(cpu_count=os.cpu_count() or 1)
    name, total, free = _nvidia_smi()
    if name:
        info.gpu_available, info.gpu_name, info.vram_total_mb, info.vram_free_mb = True, name, total, free
    try:
        import ctranslate2

        info.cuda_for_whisper = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        info.cuda_for_whisper = False
    info.ffmpeg_version, nvenc_listed = _ffmpeg_info()
    # nvenc listé ne suffit pas : il faut aussi un GPU présent
    info.nvenc_available = nvenc_listed and info.gpu_available
    if not info.gpu_available:
        info.notes.append("Aucun GPU NVIDIA détecté : transcription et encodage sur CPU.")
    if info.ffmpeg_version is None:
        info.notes.append("FFmpeg introuvable : installez-le (voir README).")
    return info


def pick_whisper_model(requested: str, hw: HardwareInfo | None = None) -> tuple[str, str, str]:
    """Retourne (modèle, device, compute_type) adaptés au matériel."""
    hw = hw or detect_hardware()
    device = "cuda" if hw.cuda_for_whisper else "cpu"
    compute = "float16" if device == "cuda" else "int8"
    if requested and requested != "auto":
        return requested, device, compute
    if device == "cuda":
        vram = hw.vram_free_mb or hw.vram_total_mb
        if vram >= 10000:
            return "large-v3", device, compute
        if vram >= 5000:
            return "medium", device, compute
        return "small", device, "int8_float16"
    # CPU : compromis vitesse/qualité
    return ("small" if hw.cpu_count >= 8 else "base"), device, compute


def pick_video_encoder(requested: str, hw: HardwareInfo | None = None) -> str:
    hw = hw or detect_hardware()
    if requested and requested != "auto":
        return requested
    return "h264_nvenc" if hw.nvenc_available else "libx264"
