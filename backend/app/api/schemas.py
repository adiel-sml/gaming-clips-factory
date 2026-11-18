from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class VideoOptions(BaseModel):
    max_video_duration: Optional[float] = Field(None, description="N'analyser que les N premières secondes")
    clips_per_video: Optional[int] = Field(None, ge=1, le=50)
    min_clip_duration: Optional[float] = Field(None, ge=5, le=180)
    max_clip_duration: Optional[float] = Field(None, ge=5, le=180)
    language: Optional[str] = None
    subtitle_style: Optional[str] = None
    aggressiveness: Optional[Literal["conservative", "normal", "aggressive"]] = None
    layout: Optional[str] = None


class VideoCreate(BaseModel):
    source: str = Field(..., description="URL (YouTube…) ou chemin d'un fichier présent sur le serveur")
    rights_confirmed: bool = Field(False, description="Je confirme avoir le droit d'utiliser cette vidéo")
    rights_note: Optional[str] = None
    title: Optional[str] = None
    options: VideoOptions = Field(default_factory=VideoOptions)
    auto: Literal["none", "analyze", "pipeline"] = "analyze"


class AnalyzeRequest(BaseModel):
    force: bool = False


class GenerateRequest(BaseModel):
    variants: Optional[list[str]] = None


class ClipPatch(BaseModel):
    start: Optional[float] = None
    end: Optional[float] = None
    layout: Optional[str] = None
    crop: Optional[dict] = Field(None, description="ex. {\"x\": 0.4} ou {\"webcam_region\": [x,y,w,h]}")
    subtitle_style: Optional[str] = None
    hook_text: Optional[str] = None
    hook_duration: Optional[float] = None
    title: Optional[str] = None
    hashtags: Optional[list[str]] = None
    status: Optional[Literal["approved", "rejected", "ready", "needs_review"]] = None
    performance: Optional[dict] = Field(None, description="Statistiques de publication (vues, likes…)")


class ExportRequest(BaseModel):
    force: bool = False
