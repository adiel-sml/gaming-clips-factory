from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_ts(seconds: float) -> str:
    """12.5 -> '00:00:12.50' ; format lisible HH:MM:SS(.cc)."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


def parse_time(value: str | float | int | None) -> float | None:
    """Heure lisible -> secondes. Accepte : 21600, "6:00:00", "62:30", "6h", "6h30", "6h30m15s", "90m", "45s".
    Retourne None si vide."""
    import re

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}(\.\d+)?", text):
        return parse_ts(text)
    # unités : 6h, 6h30, 6h30m15s, 90m, 90min, 2m30, 45s
    tokens = re.findall(r"(\d+)(h|min|m|s)?", text)
    if tokens and "".join(n + u for n, u in tokens) == text:
        factor = {"h": 3600, "min": 60, "m": 60, "s": 1}
        total, prev = 0.0, None
        for i, (num, unit) in enumerate(tokens):
            if not unit:  # nombre sans unité : seulement en dernier, après h (minutes) ou m (secondes)
                if i != len(tokens) - 1 or prev not in ("h", "m", "min"):
                    break
                unit = "m" if prev == "h" else "s"
            total += int(num) * factor[unit]
            prev = unit
        else:
            return total
    raise ValueError(f"Heure illisible : « {value} » (exemples : 6:00:00, 6h, 6h30, 90m)")


def parse_ts(value: str | float | int) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    parts = [float(p) for p in str(value).split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def file_fingerprint(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    """Empreinte rapide : taille + début + milieu + fin (évite de hasher 10 Go)."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with path.open("rb") as f:
        for offset in (0, max(0, size // 2 - chunk // 2), max(0, size - chunk)):
            f.seek(offset)
            h.update(f.read(chunk))
    return h.hexdigest()[:32]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"gcf.{name}")


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    for noisy in ("httpx", "httpx2", "huggingface_hub", "urllib3", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
