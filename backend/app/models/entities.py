"""Modèles de données (SQLModel → SQLite pour le MVP, PostgreSQL en production)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from app.core.utils import new_id, utcnow


def _json_field(default: Any = None) -> Any:
    factory = (lambda: {}) if default is None else (lambda: list(default) if isinstance(default, list) else dict(default))
    return Field(default_factory=factory, sa_column=Column(JSON))


class VideoStatus:
    DOWNLOADING = "downloading"
    IMPORTED = "imported"
    QUEUED = "queued"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    FAILED = "failed"


class ClipStatus:
    PENDING = "pending"
    RENDERING = "rendering"
    READY = "ready"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPORTED = "exported"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class Video(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("vid_"), primary_key=True)
    source_type: str = "file"                 # file | url
    source_url: Optional[str] = Field(default=None, index=True)
    title: Optional[str] = None
    file_path: Optional[str] = None
    file_hash: Optional[str] = Field(default=None, index=True)
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = True
    language: Optional[str] = None
    game: Optional[str] = None
    rights_note: Optional[str] = None
    status: str = VideoStatus.IMPORTED
    error: Optional[str] = None
    options: dict = _json_field()
    stats: dict = _json_field()               # temps de traitement, nb de mots, etc.
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Highlight(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("hl_"), primary_key=True)
    video_id: str = Field(index=True, foreign_key="video.id")
    rank: int = 0
    start: float
    end: float
    peak: float
    duration: float
    score: float
    category: str
    reason: str
    confidence: float
    transcript: str = ""
    components: dict = _json_field()
    signals: dict = _json_field()             # détails bruts (onset, mots-clés, etc.)
    status: str = "candidate"                 # candidate | rejected | generated | duplicate
    duplicate_of: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class Clip(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("clip_"), primary_key=True)
    video_id: str = Field(index=True, foreign_key="video.id")
    highlight_id: str = Field(index=True, foreign_key="highlight.id")
    variant: str = "main"
    start: float
    end: float
    layout: Optional[str] = None
    crop: dict = _json_field()                # surcharge manuelle du cadrage
    subtitle_style: Optional[str] = None
    hook: dict = _json_field()                # {text, duration, position, style, alternatives}
    titles: list = _json_field([])
    captions: list = _json_field([])
    hashtags: list = _json_field([])
    title: Optional[str] = None               # titre retenu
    status: str = ClipStatus.PENDING
    qc: dict = _json_field()
    file_path: Optional[str] = None
    thumbnail_path: Optional[str] = None
    metadata_path: Optional[str] = None
    render_seconds: float = 0.0
    duplicate_of: Optional[str] = None
    performance: dict = _json_field()         # vues, likes… (phase 3, saisie manuelle ou API officielles)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("job_"), primary_key=True)
    type: str                                  # analyze | generate | pipeline | export
    target_id: str = Field(index=True)
    payload: dict = _json_field()
    status: str = Field(default="queued", index=True)  # queued | running | done | failed
    attempts: int = 0
    error: Optional[str] = None
    result: dict = _json_field()
    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
