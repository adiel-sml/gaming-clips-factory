"""Chargement de la configuration (YAML + surcharges par variables d'environnement).

Usage :
    from app.core.config import get_settings
    s = get_settings()
    s.video.min_clip_duration
    s.get("scoring.weights.surprise", 1.0)
"""
from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

try:  # .env optionnel
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

# backend/app/core/config.py -> racine du projet = parents[3]
PROJECT_ROOT = Path(os.environ.get("GCF_ROOT", Path(__file__).resolve().parents[3]))
CONFIG_DIR = Path(os.environ.get("GCF_CONFIG_DIR", PROJECT_ROOT / "config"))


class DotDict(dict):
    """dict accessible en attributs, récursivement."""

    def __getattr__(self, item: str) -> Any:
        try:
            value = self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc
        return DotDict(value) if isinstance(value, dict) and not isinstance(value, DotDict) else value

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value

    def get_path(self, path: str, default: Any = None) -> Any:
        node: Any = self
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node


def _coerce(value: str) -> Any:
    try:
        return yaml.safe_load(value)
    except Exception:
        return value


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings(DotDict):
    def get(self, path: str, default: Any = None) -> Any:  # type: ignore[override]
        return self.get_path(path, default)

    # --- chemins dérivés -------------------------------------------------
    def _abs(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @staticmethod
    def _env_path(name: str) -> Path | None:
        value = os.environ.get(name)
        return Path(value).expanduser().resolve() if value else None  # relatif au dossier courant

    @property
    def data_dir(self) -> Path:
        d = self._env_path("GCF_DATA_DIR") or self._abs(self.get("paths.data_dir", "data"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def output_dir(self) -> Path:
        d = self._env_path("GCF_OUTPUT_DIR") or self._abs(self.get("paths.output_dir", "output"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def database_url(self) -> str:
        url = os.environ.get("DATABASE_URL") or self.get("database.url") or ""
        return url or f"sqlite:///{self.data_dir / 'gcf.db'}"

    def subtitle_presets(self) -> dict[str, dict]:
        path = CONFIG_DIR / "subtitle_presets.yaml"
        return yaml.safe_load(path.read_text()) if path.exists() else {}

    def games(self) -> dict[str, dict]:
        path = CONFIG_DIR / "games.json"
        return json.loads(path.read_text()) if path.exists() else {}


def load_settings(path: Path | None = None, overrides: dict | None = None) -> Settings:
    path = path or Path(os.environ.get("GCF_CONFIG", CONFIG_DIR / "config.yaml"))
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    # Surcharges GCF__SECTION__CLE=valeur
    for key, value in os.environ.items():
        if key.startswith("GCF__"):
            parts = [p.lower() for p in key[5:].split("__") if p]
            node = data
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = _coerce(value)
    if overrides:
        data = _deep_merge(data, overrides)
    return Settings(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings() -> None:
    get_settings.cache_clear()
