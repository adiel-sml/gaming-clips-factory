"""TranscriptionService : choisit le moteur, met en cache le résultat (transcript.json)."""
from __future__ import annotations

import time
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.hardware import pick_whisper_model
from app.core.utils import get_logger, read_json, write_json
from app.transcription.base import NullTranscriber, Segment, Transcriber, Transcript, Word, merge_fragments

log = get_logger("transcription")

_MODEL_CACHE: dict[tuple, object] = {}


def _ensure_wav16k(path: Path) -> Path:
    """Garantit un WAV 16 kHz mono 16 bits (format lu par morceaux sans tout charger en mémoire)."""
    import wave

    from app.video import ffmpeg

    path = Path(path)
    try:
        with wave.open(str(path), "rb") as w:
            if w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2:
                return path
    except Exception:
        pass
    tmp = path.with_name("audio_whisper_16k.wav")
    if not tmp.exists():
        ffmpeg.extract_audio(path, tmp, sample_rate=16000)
    return tmp


def iter_audio_chunks(path: Path, chunk_s: float = 600.0, search_s: float = 20.0):
    """Découpe le WAV en morceaux d'environ `chunk_s` secondes, coupés sur le passage le plus calme des
    `search_s` dernières secondes (pour ne pas couper un mot). Mémoire : un morceau à la fois.
    Produit (début_en_secondes, samples_float32, durée_totale)."""
    import wave

    import numpy as np

    sr = 16000
    with wave.open(str(path), "rb") as w:
        total = w.getnframes()
        pos = 0
        while pos < total:
            target = min(total, pos + int(chunk_s * sr))
            end = target
            if target < total:
                lo = max(pos + sr, target - int(search_s * sr))
                w.setpos(lo)
                tail = np.frombuffer(w.readframes(target - lo), dtype=np.int16).astype(np.float32)
                hop = sr // 10
                if tail.size >= hop:
                    rms = [float(np.mean(tail[i:i + hop] ** 2)) for i in range(0, tail.size - hop + 1, hop)]
                    end = lo + int(np.argmin(rms)) * hop + hop // 2
            w.setpos(pos)
            samples = np.frombuffer(w.readframes(end - pos), dtype=np.int16).astype(np.float32) / 32768.0
            yield pos / sr, samples, total / sr
            pos = end


class FasterWhisperTranscriber:
    """faster-whisper (CTranslate2) avec horodatage au mot. GPU utilisé automatiquement si dispo."""

    def __init__(self, model: str = "auto", beam_size: int = 1, vad_filter: bool = True, hotwords: str | None = None,
                 chunk_seconds: float = 120.0):
        self.model_name, self.device, self.compute_type = pick_whisper_model(model)
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.hotwords = hotwords or None
        self.chunk_seconds = chunk_seconds
        self.name = f"faster-whisper:{self.model_name}"

    def _model(self):
        key = (self.model_name, self.device, self.compute_type)
        if key not in _MODEL_CACHE:
            from faster_whisper import WhisperModel

            from app.core.hardware import detect_hardware

            log.info("Chargement Whisper %s (%s, %s) — RAM disponible : %s Mo", *key,
                     detect_hardware().memory_limit_mb or "?")
            _MODEL_CACHE[key] = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        return _MODEL_CACHE[key]

    def transcribe(self, audio_path: Path, language: str | None = None, progress=None) -> Transcript:
        """Transcription par morceaux de ~2 min : la mémoire ne dépend pas de la durée de la vidéo.
        L'audio est passé décodé (pas de dépendance au décodeur PyAV)."""
        lang = None if language in (None, "", "auto") else language
        wav = _ensure_wav16k(Path(audio_path))
        segments: list[Segment] = []
        detected = lang
        for offset, samples, total in iter_audio_chunks(wav, float(self.chunk_seconds)):
            segments_iter, info = self._model().transcribe(
                samples, language=detected, word_timestamps=True, beam_size=self.beam_size,
                vad_filter=self.vad_filter, condition_on_previous_text=False, hotwords=self.hotwords,
            )
            detected = detected or info.language   # même langue pour tous les morceaux
            for seg in segments_iter:
                if progress and total:
                    progress(min(1.0, (offset + seg.end) / total))
                words = [Word(round(offset + w.start, 3), round(offset + w.end, 3), w.word.strip(), round(w.probability, 3))
                         for w in (seg.words or []) if w.word.strip()]
                words = merge_fragments(words)
                segments.append(Segment(round(offset + seg.start, 3), round(offset + seg.end, 3), seg.text.strip(), words))
            del samples
        return Transcript(language=detected or "unknown", segments=segments, model=self.name)


def build_transcriber(settings: Settings | None = None) -> Transcriber:
    s = settings or get_settings()
    backend = s.get("transcription.backend", "faster_whisper")
    if backend == "none":
        return NullTranscriber()
    hints: list[str] = []
    if s.get("transcription.hotwords", True):
        for g in s.games().values():
            hints += g.get("asr_hints", g.get("keywords", [])[:2])
    return FasterWhisperTranscriber(
        model=s.get("transcription.model", "auto"),
        beam_size=int(s.get("transcription.beam_size", 1)),
        vad_filter=bool(s.get("transcription.vad_filter", True)),
        hotwords=", ".join(dict.fromkeys(hints))[:400] or None,
        chunk_seconds=float(s.get("transcription.chunk_seconds", 120)),
    )


class TranscriptionService:
    def __init__(self, transcriber: Transcriber | None = None, settings: Settings | None = None):
        self.s = settings or get_settings()
        self._transcriber = transcriber

    @property
    def transcriber(self) -> Transcriber:
        if self._transcriber is None:
            self._transcriber = build_transcriber(self.s)
        return self._transcriber

    def transcribe(self, audio_path: Path, work_dir: Path, language: str | None = None,
                   force: bool = False, progress=None) -> Transcript:
        cache = work_dir / "transcript.json"
        if not force and (cached := read_json(cache)):
            return Transcript.from_dict(cached)
        t0 = time.time()
        lang = language or self.s.get("transcription.language", "auto")
        import inspect

        if progress and "progress" in inspect.signature(self.transcriber.transcribe).parameters:
            transcript = self.transcriber.transcribe(audio_path, lang, progress=progress)
        else:
            transcript = self.transcriber.transcribe(audio_path, lang)
        log.info("Transcription : %d segments, langue=%s, %.1fs", len(transcript.segments),
                 transcript.language, time.time() - t0)
        write_json(cache, transcript.to_dict())
        return transcript
