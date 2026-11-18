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


def _load_audio(path: Path):
    """WAV 16 kHz mono -> tableau float32 attendu par faster-whisper (sinon conversion FFmpeg)."""
    from app.video import ffmpeg

    try:
        samples, sr = ffmpeg.read_wav_mono(Path(path))
    except Exception:
        samples, sr = None, 0
    if samples is None or sr != 16000:
        tmp = Path(path).with_name("audio_whisper_16k.wav")
        ffmpeg.extract_audio(Path(path), tmp, sample_rate=16000)
        samples, sr = ffmpeg.read_wav_mono(tmp)
    return samples


class FasterWhisperTranscriber:
    """faster-whisper (CTranslate2) avec horodatage au mot. GPU utilisé automatiquement si dispo."""

    def __init__(self, model: str = "auto", beam_size: int = 1, vad_filter: bool = True, hotwords: str | None = None):
        self.model_name, self.device, self.compute_type = pick_whisper_model(model)
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.hotwords = hotwords or None
        self.name = f"faster-whisper:{self.model_name}"

    def _model(self):
        key = (self.model_name, self.device, self.compute_type)
        if key not in _MODEL_CACHE:
            from faster_whisper import WhisperModel

            log.info("Chargement Whisper %s (%s, %s)", *key)
            _MODEL_CACHE[key] = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        return _MODEL_CACHE[key]

    def transcribe(self, audio_path: Path, language: str | None = None) -> Transcript:
        lang = None if language in (None, "", "auto") else language
        # On passe l'audio déjà décodé (WAV 16 kHz mono) : pas de dépendance au décodeur PyAV,
        # dont certaines versions cassent faster-whisper.
        audio = _load_audio(audio_path)
        segments_iter, info = self._model().transcribe(
            audio, language=lang, word_timestamps=True, beam_size=self.beam_size,
            vad_filter=self.vad_filter, condition_on_previous_text=False, hotwords=self.hotwords,
        )
        segments: list[Segment] = []
        for s in segments_iter:
            words = [Word(round(w.start, 3), round(w.end, 3), w.word.strip(), round(w.probability, 3))
                     for w in (s.words or []) if w.word.strip()]
            words = merge_fragments(words)
            segments.append(Segment(round(s.start, 3), round(s.end, 3), s.text.strip(), words))
        return Transcript(language=info.language, segments=segments, model=self.name)


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
                   force: bool = False) -> Transcript:
        cache = work_dir / "transcript.json"
        if not force and (cached := read_json(cache)):
            return Transcript.from_dict(cached)
        t0 = time.time()
        lang = language or self.s.get("transcription.language", "auto")
        transcript = self.transcriber.transcribe(audio_path, lang)
        log.info("Transcription : %d segments, langue=%s, %.1fs", len(transcript.segments),
                 transcript.language, time.time() - t0)
        write_json(cache, transcript.to_dict())
        return transcript
