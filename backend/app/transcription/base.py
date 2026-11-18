"""Interface de transcription : tout moteur (faster-whisper, API externe…) implémente `Transcriber`."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    language: str
    segments: list[Segment]
    model: str = ""

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    def text_between(self, start: float, end: float) -> str:
        return join_words([w.text for w in self.words if w.end > start and w.start < end])

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words if w.end > start and w.start < end]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        segs = [Segment(s["start"], s["end"], s["text"], [Word(**w) for w in s.get("words", [])])
                for s in d.get("segments", [])]
        return cls(language=d.get("language", "unknown"), segments=segs, model=d.get("model", ""))


def join_words(tokens: list[str]) -> str:
    """Recolle la ponctuation et les élisions (« l 'eau » -> « l'eau », « regardez -le » -> « regardez-le »)."""
    out = ""
    for tok in (t.strip() for t in tokens):
        if not tok:
            continue
        if out and (tok[0] in "'’-,.!?…:;)" or out.endswith(("'", "’", "-", "("))):
            out += tok
        else:
            out += (" " if out else "") + tok
    return out


def merge_fragments(words: list[Word]) -> list[Word]:
    """Fusionne les fragments de mots produits par Whisper (« Aujourd » + « 'hui »)."""
    merged: list[Word] = []
    for w in words:
        t = w.text.strip()
        if merged and t and (t[0] in "'’-" or merged[-1].text.endswith(("'", "’"))) and len(t) > 1:
            prev = merged[-1]
            merged[-1] = Word(prev.start, w.end, prev.text + t, min(prev.prob, w.prob))
        elif merged and t and all(c in ",.!?…:;" for c in t):
            prev = merged[-1]
            merged[-1] = Word(prev.start, w.end, prev.text + t, prev.prob)
        else:
            merged.append(Word(w.start, w.end, t, w.prob))
    return merged


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio_path: Path, language: str | None = None) -> Transcript: ...


class NullTranscriber:
    """Utilisé quand la transcription est désactivée ou pour des vidéos sans voix."""

    name = "none"

    def transcribe(self, audio_path: Path, language: str | None = None) -> Transcript:
        return Transcript(language=language or "unknown", segments=[], model="none")
